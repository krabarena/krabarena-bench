// Playwright client driver shared by both runners.
//
// Invoked as:
//   node /task/harness/drive.js \
//     --task <id> \
//     --target <fixture-url> \
//     --browser-endpoint <ws-or-http-url> \
//     --connect-mode <playwright|cdp> \
//     [--inputs <json-string>] \
//     [--expected <json-string>]
//
// Writes a JSON object to /results/output.json with task-specific
// fields. After the handler returns, every key in `expected` is
// asserted against the corresponding key in `output`; any mismatch
// makes the process exit 1 so `RunResult.success` reflects whether
// the task actually produced the right answer (bench-kit's
// `success_predicate` is parsed but not yet evaluated server-side,
// so the harness enforces it locally — see README §Caveats).

"use strict";

const fs = require("node:fs/promises");
const path = require("node:path");
const { chromium } = require("playwright");

function parseArgs(argv) {
  const out = {};
  for (let i = 2; i < argv.length; i++) {
    const k = argv[i];
    if (k.startsWith("--")) {
      out[k.slice(2)] = argv[++i];
    }
  }
  return out;
}

async function connect(endpoint, mode) {
  if (mode === "playwright") {
    return chromium.connect(endpoint);
  }
  if (mode === "cdp") {
    return chromium.connectOverCDP(endpoint);
  }
  throw new Error(`unknown connect-mode: ${mode}`);
}

async function newPage(browser) {
  const ctx = await browser.newContext();
  return ctx.newPage();
}

const TASKS = {
  "static-spa": async (browser, target) => {
    const page = await newPage(browser);
    await page.goto(target, { waitUntil: "networkidle" });
    // The fixture sets dataset.ready synchronously after rendering its
    // 50 list items; assert it explicitly so a runtime that resolves
    // `goto` before the inline script finishes (e.g. some CDP-only
    // implementations) still produces the right item count.
    await page.waitForFunction(() => document.body.dataset.ready === "1");
    const title = await page.title();
    const items = await page.locator("#items li").count();
    return { title, items };
  },

  "form-fill": async (browser, target, inputs) => {
    const page = await newPage(browser);
    await page.goto(target);
    await page.fill("#username", inputs.username);
    await page.fill("#password", inputs.password);
    await Promise.all([page.waitForURL(/\/form\/ok/), page.click("#submit")]);
    const heading = (await page.textContent("h1"))?.trim();
    return { heading };
  },

  "infinite-scroll": async (browser, target, inputs) => {
    const page = await newPage(browser);
    await page.goto(target);
    const target_count = inputs.target_count || 200;
    // Click + waitForFunction(li count >= target) — each runtime
    // advances at its own pace and we measure that. The previous
    // `waitForTimeout(50)` floor-padded both runtimes to ~500 ms of
    // pure sleep on this task, masking real differences.
    let current = await page.locator("#items li").count();
    while (current < target_count) {
      const next = current + 20;
      await page.click("#more");
      await page.waitForFunction(
        (n) => document.querySelectorAll("#items li").length >= n,
        next,
      );
      current = next;
    }
    // Hash the harvested texts in DOM order, joined with `\n`.
    // Catches "right count, wrong content" where a runtime renders
    // the items but with mangled text — the count predicate alone
    // would silently pass.
    const texts = await page.$$eval("#items li", (els) =>
      els.map((el) => el.textContent.trim()),
    );
    const { createHash } = require("node:crypto");
    const items_hash = createHash("sha256").update(texts.join("\n")).digest("hex");
    return { count: current, items_hash };
  },

  "xhr-driven": async (browser, target) => {
    const page = await newPage(browser);
    await page.goto(target);
    await page.click("#load");
    await page.waitForFunction(() => document.body.dataset.loaded === "1");
    const count = await page.locator("#items li").count();
    return { count };
  },

  "multi-tab": async (browser, target, inputs) => {
    const concurrency = inputs.concurrency || 10;
    const ctxs = await Promise.all(
      Array.from({ length: concurrency }, () => browser.newContext()),
    );
    const counts = await Promise.all(
      ctxs.map(async (ctx) => {
        const page = await ctx.newPage();
        await page.goto(target, { waitUntil: "networkidle" });
        await page.waitForFunction(() => document.body.dataset.ready === "1");
        return page.locator("#items li").count();
      }),
    );
    await Promise.all(ctxs.map((c) => c.close()));
    return { total_items: counts.reduce((a, b) => a + b, 0) };
  },

  "canvas-render": async (browser, target) => {
    const page = await newPage(browser);
    await page.goto(target);
    await page.waitForFunction(() => document.body.dataset.painted === "1");
    const dataUrl = await page.evaluate(() =>
      document.getElementById("c").toDataURL(),
    );
    const { createHash } = require("node:crypto");
    const canvas_hash = createHash("sha256").update(dataUrl).digest("hex");
    return { canvas_hash };
  },

  "client-router": async (browser, target) => {
    const page = await newPage(browser);
    await page.goto(target);
    await page.click("#link-about");
    await page.waitForFunction(
      () => document.body.dataset.route === "#/about",
    );
    const heading = (await page.textContent("h1"))?.trim();
    return { heading };
  },
};

async function writeOutput(payload) {
  const outPath = path.join("/results", "output.json");
  await fs.writeFile(outPath, JSON.stringify(payload) + "\n", "utf-8");
}

function findExpectedMismatch(output, expected) {
  // Generic predicate-shaped check: for every key in `expected` that
  // also exists in `output`, the values must be deep-equal. Keys in
  // `expected` that are absent from `output` are treated as
  // documentation-only placeholders (e.g. `hash_present: true` on
  // canvas-render before the golden hash is pinned).
  if (!expected || typeof expected !== "object") return null;
  for (const [key, want] of Object.entries(expected)) {
    if (!(key in output)) continue;
    const got = output[key];
    if (JSON.stringify(got) !== JSON.stringify(want)) {
      return { key, expected: want, got };
    }
  }
  return null;
}

async function main() {
  const args = parseArgs(process.argv);
  const inputs = args.inputs ? JSON.parse(args.inputs) : {};
  const expected = args.expected ? JSON.parse(args.expected) : {};
  const handler = TASKS[args.task];
  if (!handler) {
    throw new Error(`unknown task: ${args.task}`);
  }
  let browser;
  try {
    browser = await connect(args["browser-endpoint"], args["connect-mode"]);
    const output = await handler(browser, args.target, inputs);
    await writeOutput(output);
    const mismatch = findExpectedMismatch(output, expected);
    if (mismatch) {
      console.error(
        `task ${args.task}: expected.${mismatch.key}=${JSON.stringify(
          mismatch.expected,
        )} but got ${JSON.stringify(mismatch.got)}`,
      );
      process.exit(1);
    }
  } catch (err) {
    // Always emit output.json with diagnostic context so the
    // orchestrator log carries the failure cause; exit non-zero
    // so the runner records `success: false`.
    await writeOutput({
      error: {
        task: args.task,
        target: args.target,
        message: String(err && err.message ? err.message : err),
        stack: err && err.stack ? err.stack : null,
      },
    }).catch(() => {});
    console.error(err && err.stack ? err.stack : String(err));
    process.exit(1);
  } finally {
    if (browser) await browser.close().catch(() => {});
  }
}

main();
