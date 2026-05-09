"""Lightpanda runner — drives the Zig-based engine over raw CDP."""

from __future__ import annotations

import json

from bench_kit.exec import run_task_in_sandbox
from bench_kit.runner_base import Runner, RunResult, Task

# Same harness image as the browserless runner; symmetry across
# tools is editorial. See runners/browserless.py for the rationale
# and publish workflow.
_HARNESS_IMAGE = (
    "ghcr.io/keenableai/krabarena-bench-browsers-harness"
    "@sha256:f612952c0c09b609dcf428f63d1ac44b93d1fdbfda78f0c7bf35e1b5d35b23ed"
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
