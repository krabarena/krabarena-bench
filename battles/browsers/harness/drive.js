// Playwright client driver shared by both runners.
//
// Invoked as:
//   node /task/harness/drive.js \
//     --task <id> \
//     --target <fixture-url> \
//     --browser-endpoint <ws-or-http-url> \
//     --connect-mode <playwright|cdp> \
//     [--inputs <json-string>]
//
// Writes a JSON object to /results/output.json with task-specific
// fields the orchestrator's success_predicate is evaluated against.
// Exits 0 on success, 1 on any task-level failure (the wrapper still
// records metrics + bundles the log).

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
    return { count: current };
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

async function main() {
  const args = parseArgs(process.argv);
  const inputs = args.inputs ? JSON.parse(args.inputs) : {};
  const handler = TASKS[args.task];
  if (!handler) {
    throw new Error(`unknown task: ${args.task}`);
  }
  let browser;
  try {
    browser = await connect(args["browser-endpoint"], args["connect-mode"]);
    const output = await handler(browser, args.target, inputs);
    await writeOutput(output);
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
