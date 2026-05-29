"""Selenium runner — drives Chromium via Selenium 4's WebDriver-Grid CDP forwarder.

Selected from the WebQL competitor survey as the industry-standard
test-infra incumbent. Selenium 4 exposes a Chrome session's CDP
endpoint as the ``se:cdp`` capability after the session is created,
and Playwright's ``chromium.connectOverCDP`` can attach to it. This
runner does that handshake.

## Flow

The shared drive.js only understands HTTP-or-WS browser endpoints; it
doesn't know how to ask a Selenium grid to spin up a session. So the
runner does the session bootstrap *outside* drive.js:

1. Wrap the harness invocation in a small shell preflight that
   POSTs to Selenium's ``/wd/hub/session`` endpoint with a minimal
   Chrome capabilities body.
2. Parse the returned ``value.capabilities.se:cdp`` (a ws:// URL)
   with python.
3. ``exec node /task/harness/drive.js --browser-endpoint <ws-url>
   --connect-mode cdp …`` — Playwright sees a ws:// URL and connects
   directly, skipping the /json/version dance entirely (which is
   important because Selenium's gateway doesn't expose /json/version
   on the grid root, only via the per-session /se/cdp proxy).

The session is implicitly cleaned up when the test container exits
(Selenium reaps idle sessions). For 5-iteration tasks we eat the
session-creation cost (~1-2s) per iteration; that's fair — every
Selenium-based agent client pays this cost.

## Why seleniarm and not selenium/standalone-chrome

Google doesn't publish arm64 Chromium builds, so the upstream
selenium/standalone-chrome image is amd64-only. seleniarm is the
community arm64 fork; same Selenium 4 server, identical ``se:cdp``
capability surface. The pinned digest below is the multi-arch
manifest list at the time of this commit.
"""

from __future__ import annotations

import json

from bench_kit.exec import run_task_in_sandbox
from bench_kit.runner_base import Runner, RunResult, Task

_HARNESS_IMAGE = (
    "ghcr.io/krabarena/krabarena-bench-browsers-harness"
    "@sha256:7e91d0edd21b24332e21d657330a0f6c48d7450e51b219b2b53bb9b646ee9e7d"
)
_GRID_URL = "http://selenium:4444"

# Preflight inside the harness container: open a Selenium session,
# extract se:cdp, exec drive.js pointed at it.
_PREFLIGHT = f"""set -e
SESSION=$(curl -fsS -X POST {_GRID_URL}/wd/hub/session \\
  -H 'Content-Type: application/json' \\
  -d '{{"capabilities":{{"alwaysMatch":{{"browserName":"chrome","goog:chromeOptions":{{"args":["--no-sandbox","--disable-dev-shm-usage"]}}}}}}}}')
# Selenium standalone advertises se:cdp with its raw bridge IP
# (172.23.0.x) instead of the Compose service name. Rewrite the
# host portion so the WS connection routes via DNS through the
# bench network. Path (/session/<id>/se/cdp) is preserved verbatim.
PARSED=$(echo "$SESSION" | python3 -c '
import json, sys, re
d = json.load(sys.stdin)
sid = d["value"]["sessionId"]
ws = d["value"]["capabilities"]["se:cdp"]
ws = re.sub(r"ws://[^/]+", "ws://selenium:4444", ws)
print(sid, ws)
')
SESSION_ID=$(echo "$PARSED" | cut -d' ' -f1)
WS=$(echo "$PARSED" | cut -d' ' -f2)
# Always release the session on exit (success OR failure) so each
# task hands a fresh slot back to the Selenium pool. Without this
# the SE_NODE_SESSION_TIMEOUT (30s) would still reap eventually,
# but explicit DELETE is cleaner + immediate.
trap 'curl -fsS -X DELETE {_GRID_URL}/wd/hub/session/$SESSION_ID >/dev/null 2>&1 || true' EXIT
node /task/harness/drive.js "$@" --browser-endpoint "$WS"
"""


class SeleniumRunner(Runner):
    name = "selenium"
    image = (
        "seleniarm/standalone-chromium"
        "@sha256:d644a5f679e83e63344cee11c08fc2c7bf4acf43217434a8621a2bc85f7473a5"
    )

    def run(self, task: Task) -> RunResult:
        return run_task_in_sandbox(
            task,
            image=_HARNESS_IMAGE,
            args=[
                "sh",
                "-c",
                _PREFLIGHT,
                "_preflight",
                "--task",
                task.id,
                "--target",
                str(task.inputs.get("url", "")),
                "--connect-mode",
                "cdp",
                "--inputs",
                json.dumps(task.inputs),
                "--expected",
                json.dumps(task.expected),
            ],
        )
