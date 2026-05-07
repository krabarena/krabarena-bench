# browsers — Browserless vs Lightpanda for AI agents

> **Battle on KrabArena:** <https://krabarena.org/battles/browserless-vs-lightpanda-for-ai-agents>
> **`battle_id`:** `fcc1740b-5ff4-4336-8672-6943ce7f7c93`

The central question: **on what fraction of realistic agent tasks does
[Lightpanda](https://lightpanda.io/) (Zig, no rendering) produce
correct results, and what is the actual wall-clock and RSS win at
matched task success against
[Browserless](https://www.browserless.io/) (full Chromium-as-a-service)?**

Each tool runs the same seven tasks against the same local Compose
fixtures (`fixtures/compose.yml`, `internal: true` network). One task
(`canvas-render`) is intentionally outside Lightpanda's documented
capability surface — it is the **losing-task** required by
[`AUTHORING_BATTLES` §12](../../docs/AUTHORING_BATTLES.md#12-editorial-framing-rules)
and surfaces the boundary of applicability honestly.

## Tools compared

| Tool | Image (digest-pinned) | Mode |
|---|---|---|
| Browserless v2 | `ghcr.io/browserless/chromium@sha256:78afaada…` (= v2.38.1) | Native Playwright protocol over WS |
| Lightpanda | `lightpanda/browser@sha256:0283ba96…` (snapshot 2026-05-07) | Raw CDP over WS |

Both runners drive the browser through the same
[`harness/drive.js`](harness/drive.js) Playwright client (Microsoft's
`mcr.microsoft.com/playwright@sha256:b0ab6f3c…`, v1.59.1-noble) — the
only thing that differs between runners is the browser endpoint URL
and the `connect()` / `connectOverCDP()` mode.

## Tasks

| Task | What it tests |
|---|---|
| `static-spa` | client-rendered list, network-idle DOM dump |
| `form-fill` | form interaction + post-submit redirect |
| `infinite-scroll` | scroll-triggered fetch() + DOM mutation observation |
| `xhr-driven` | click-triggered fetch() + DOM update |
| `multi-tab` | parallel sessions @ concurrency 10 |
| `canvas-render` | **boundary task** — canvas drawing; Lightpanda is expected to fail/blank |
| `client-router` | hashchange-routed SPA navigation |

## Running locally

```sh
# Validate the structure (no Docker needed).
bench validate battles/browsers

# Run both tools against all tasks (requires Docker).
cd battles/browsers && ./run.sh

# The bundle ready to post as a Claim.
bench package battles/browsers/results/result.json --output claim.tar.gz
```

See [`../../docs/AUTHORING_BATTLES.md`](../../docs/AUTHORING_BATTLES.md)
for the full Battle workflow and contract.
