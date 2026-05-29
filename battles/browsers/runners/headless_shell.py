"""Headless Shell runner — drives raw upstream Chromium over CDP.

The "control" experiment for this Battle: chromedp/headless-shell is
an unwrapped Chromium headless binary with --remote-debugging-port
exposed, no session manager, no queueing, no anti-bot layer. Pairing
it against Browserless (a full service-layer wrapper on the same
upstream Chromium) measures the overhead Browserless's wrapper adds.

Added as part of the WebQL-driven competitor survey documented in
the Battle's accompanying claims.

## Why a front-door proxy is necessary

Raw Chrome's CDP server enforces DNS-rebinding protection on BOTH
the HTTP /json/version endpoint AND the WebSocket upgrade: any
request whose Host header isn't an IP or "localhost" gets
``HTTP 500 — Host header is specified and is not an IP address or
localhost``. From inside the Compose bench network the harness
container can only resolve the browser via a service hostname, so
its Host header is the service name and Chrome rejects every
connection. Lightpanda's CDP server is permissive about Host headers
and doesn't trip this; raw Chrome does.

The bench resolves this with an nginx front-door (see
``battles/browsers/fixtures/nginx.conf``). The compose file aliases
``headless-shell`` to the nginx service; the raw chrome image runs
as ``chrome-internal``. nginx rewrites the Host header to
``localhost:9222`` on every request (including the WS upgrade) and
substitutes ``ws://localhost:9222`` for ``ws://headless-shell:9222``
in /json/version responses so Playwright's discovery routes back
through the proxy. From the runner's perspective the endpoint looks
identical to Lightpanda's: a plain HTTP URL that connectOverCDP
discovers + connects on.
"""

from __future__ import annotations

import json

from bench_kit.exec import run_task_in_sandbox
from bench_kit.runner_base import Runner, RunResult, Task

_HARNESS_IMAGE = (
    "ghcr.io/krabarena/krabarena-bench-browsers-harness"
    "@sha256:7e91d0edd21b24332e21d657330a0f6c48d7450e51b219b2b53bb9b646ee9e7d"
)
_BROWSER_ENDPOINT = "http://headless-shell:9222"


class HeadlessShellRunner(Runner):
    name = "headless-shell"
    image = (
        "chromedp/headless-shell"
        "@sha256:313ed7255ae1e155fb157631a6d4c0eb8b65bbe06de9e704ed834399bdf678ff"
    )

    def run(self, task: Task) -> RunResult:
        return run_task_in_sandbox(
            task,
            image=_HARNESS_IMAGE,
            args=[
                "node",
                "/task/harness/drive.js",
                "--task",
                task.id,
                "--target",
                str(task.inputs.get("url", "")),
                "--browser-endpoint",
                _BROWSER_ENDPOINT,
                "--connect-mode",
                "cdp",
                "--inputs",
                json.dumps(task.inputs),
                "--expected",
                json.dumps(task.expected),
            ],
        )
