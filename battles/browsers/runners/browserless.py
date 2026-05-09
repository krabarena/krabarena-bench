"""Browserless v2 runner — drives a managed Chromium over Playwright WS."""

from __future__ import annotations

import json

from bench_kit.exec import run_task_in_sandbox
from bench_kit.runner_base import Runner, RunResult, Task

# Our own thin layer over `mcr.microsoft.com/playwright` adding the
# `playwright` npm package — the upstream MCR image ships browsers
# but not the SDK. Built and pushed by GHA on changes to
# ``battles/browsers/harness/`` (see
# ``.github/workflows/publish-browsers-harness.yml``); pinned by
# manifest digest so the same bytes run for claimer and verifier.
# The image we're *benchmarking* is `image` below — this is purely
# the harness driving it.
_HARNESS_IMAGE = (
    "ghcr.io/keenableai/krabarena-bench-browsers-harness"
    "@sha256:06df2018065532f00294cd4ba26cd1010238e3f00b38985c341f4e3f99a05c99"
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
                "playwright",
                "--inputs",
                json.dumps(task.inputs),
                "--expected",
                json.dumps(task.expected),
            ],
        )
