"""Browserless v2 runner — drives a managed Chromium over Playwright WS."""

from __future__ import annotations

import json

from bench_kit.exec import run_task_in_sandbox
from bench_kit.runner_base import Runner, RunResult, Task

# Microsoft's Playwright runtime. The image being benchmarked is the
# browser (see ``image`` below); this is just the harness-side library
# that speaks to it. Pinned by digest for reproducibility.
_PLAYWRIGHT_IMAGE = (
    "mcr.microsoft.com/playwright"
    "@sha256:b0ab6f3cb99aa7803adbc14d9027ec1785fc6e433b97e134e0f8fe61683b6b53"
)
_BROWSER_ENDPOINT = "ws://browserless:3000?token=krabarena"


class BrowserlessRunner(Runner):
    name = "browserless"
    image = (
        "ghcr.io/browserless/chromium"
        "@sha256:78afaada9f7b049783bfed624e6b5e9a2d3438fc04bb46801ed777e82ae1501f"
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
                "playwright",
                "--inputs",
                json.dumps(task.inputs),
                "--expected",
                json.dumps(task.expected),
            ],
        )
