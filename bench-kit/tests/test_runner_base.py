"""Smoke tests for the public Runner ABI."""

from __future__ import annotations

from pathlib import Path

import pytest

from bench_kit.runner_base import RunMetrics, Runner, RunResult, Task, TaskLimits


def test_runner_is_abstract() -> None:
    with pytest.raises(TypeError):
        Runner()  # type: ignore[abstract]  # pyright: ignore[reportAbstractUsage]


def test_task_is_frozen() -> None:
    task = Task(
        id="example",
        title="t",
        description="d",
        fixture_service="svc",
        inputs={},
        expected={},
        success_predicate="True",
        timeout_s=10,
        iterations=1,
        limits=TaskLimits(memory_mb=256, cpus=1.0),
    )
    with pytest.raises(AttributeError):
        task.id = "other"  # type: ignore[misc]


def test_run_result_construction() -> None:
    res = RunResult(
        success=True,
        metrics=RunMetrics(wall_clock_ms=10, peak_rss_mb=20, cpu_time_ms=5),
        logs_path=Path("runs/example/0"),
    )
    assert res.success
    assert res.metrics.wall_clock_ms == 10


def test_concrete_subclass_can_be_instantiated() -> None:
    class FakeRunner(Runner):
        name = "fake"
        image = "fake@sha256:" + "0" * 64

        def run(self, task: Task) -> RunResult:
            return RunResult(
                success=True,
                metrics=RunMetrics(wall_clock_ms=1, peak_rss_mb=1, cpu_time_ms=1),
                logs_path=Path("runs/fake/0"),
            )

    assert FakeRunner().name == "fake"
