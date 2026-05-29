"""Headless Shell runner — drives raw upstream Chromium over CDP.

The "control" experiment for this Battle: chromedp/headless-shell is
an unwrapped Chromium headless binary with --remote-debugging-port
exposed, no session manager, no queueing, no anti-bot layer. Pairing
it against Browserless (a full service-layer wrapper on the same
upstream Chromium) measures the overhead Browserless's wrapper adds.

Added to this Battle as part of the WebQL-driven competitor survey
documented in the Battle's accompanying claims.
"""

from __future__ import annotations

import json

from bench_kit.exec import run_task_in_sandbox
from bench_kit.runner_base import Runner, RunResult, Task

# Same harness as the other runners. See runners/browserless.py for
# the publish workflow rationale.
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
