"""Lightpanda runner — drives the Zig-based engine over raw CDP."""

from __future__ import annotations

import json

from bench_kit.exec import run_task_in_sandbox
from bench_kit.runner_base import Runner, RunResult, Task

_PLAYWRIGHT_IMAGE = (
    "mcr.microsoft.com/playwright"
    "@sha256:b0ab6f3cb99aa7803adbc14d9027ec1785fc6e433b97e134e0f8fe61683b6b53"
)
_BROWSER_ENDPOINT = "http://lightpanda:9222"


class LightpandaRunner(Runner):
    name = "lightpanda"
    image = (
        "lightpanda/browser"
        "@sha256:0283ba962dffe2f7b46a29377276837b033b1174b92b5a30da641a1b1561ef5c"
    )

    def run(self, task: Task) -> RunResult:
        return run_task_in_sandbox(
            task,
            image=_PLAYWRIGHT_IMAGE,
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
