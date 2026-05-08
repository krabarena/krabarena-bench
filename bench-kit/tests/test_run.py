"""Tests for the orchestrator (``bench_kit.run``).

Most of the orchestrator's behaviour is checked through small,
docker-free unit tests:

* Task / runner discovery from a synthetic battle dir.
* Summary aggregation across iterations.
* Schema-conformance of the synthesised ``result.json`` (with a
  fake runner that returns hard-coded metrics).

The full docker-fixtures path is exercised by ``test_run_real_docker``
(behind ``@pytest.mark.docker``) and ultimately by the per-Battle
integration in PR 4+.
"""

from __future__ import annotations

import json
import textwrap
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
import yaml
from jsonschema import Draft202012Validator

from bench_kit.init import init_battle
from bench_kit.run import (
    RunError,
    RunOptions,
    _build_summary,
    _load_runners,
    _load_tasks,
    _matches_expected,
    _percentile,
    run_battle,
)
from bench_kit.runner_base import RunMetrics, RunResult, Task
from bench_kit.schemas import load_schema

_GOOD_UUID = "550e8400-e29b-41d4-a716-446655440000"
_DIGEST = "sha256:" + "0" * 64
_VALID_IMAGE = f"library/hello-world@{_DIGEST}"


# ---------------------------------------------------------------------------
# task / runner discovery
# ---------------------------------------------------------------------------


def test_load_tasks_returns_by_id(tmp_path: Path) -> None:
    target = init_battle("browsers", _GOOD_UUID, parent_dir=tmp_path)
    tasks = _load_tasks(target / "tasks")
    assert "example" in tasks
    assert tasks["example"]["fixture_service"] == "example"


def test_load_tasks_empty_dir_raises(tmp_path: Path) -> None:
    (tmp_path / "tasks").mkdir()
    with pytest.raises(RunError, match="no tasks found"):
        _load_tasks(tmp_path / "tasks")


def test_load_runners_finds_subclass(tmp_path: Path) -> None:
    init_battle("browsers", _GOOD_UUID, parent_dir=tmp_path)
    runners_dir = tmp_path / "battles" / "browsers" / "runners"
    runners = _load_runners(runners_dir)
    assert "example" in runners


def test_load_runners_two_classes_in_one_file_rejected(tmp_path: Path) -> None:
    runners_dir = tmp_path / "runners"
    runners_dir.mkdir()
    (runners_dir / "two.py").write_text(
        textwrap.dedent(
            """
            from bench_kit.runner_base import Runner, RunResult, Task

            class A(Runner):
                name = "a"
                image = "lib/a@"""
            + _DIGEST
            + """"
                def run(self, task): raise NotImplementedError

            class B(Runner):
                name = "b"
                image = "lib/b@"""
            + _DIGEST
            + """"
                def run(self, task): raise NotImplementedError
            """
        ).lstrip(),
        encoding="utf-8",
    )
    with pytest.raises(RunError, match="more than one Runner"):
        _load_runners(runners_dir)


def test_load_runners_no_subclass_rejected(tmp_path: Path) -> None:
    runners_dir = tmp_path / "runners"
    runners_dir.mkdir()
    (runners_dir / "empty.py").write_text("# nothing here\n", encoding="utf-8")
    with pytest.raises(RunError, match="no Runner subclass"):
        _load_runners(runners_dir)


# ---------------------------------------------------------------------------
# percentile + summary
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("values", "p", "expected"),
    [
        ([10], 50, 10),
        ([10, 20, 30, 40, 50], 50, 30),
        ([10, 20, 30, 40, 50], 95, 50),
        ([10, 20, 30, 40, 50], 99, 50),
        ([], 50, 0),
    ],
)
def test_percentile(values: list[int], p: int, expected: int) -> None:
    assert _percentile(sorted(values), p) == expected


def test_summary_groups_tool_task_with_metrics() -> None:
    runs = [
        {
            "tool": "fast",
            "task": "scroll",
            "iteration": 0,
            "metrics": {"wall_clock_ms": 100, "peak_rss_mb": 10, "cpu_time_ms": 50},
            "success": True,
            "logs_path": "runs/fast/scroll/0",
        },
        {
            "tool": "fast",
            "task": "scroll",
            "iteration": 1,
            "metrics": {"wall_clock_ms": 110, "peak_rss_mb": 12, "cpu_time_ms": 55},
            "success": True,
            "logs_path": "runs/fast/scroll/1",
        },
        {
            "tool": "fast",
            "task": "scroll",
            "iteration": 2,
            "metrics": {"wall_clock_ms": 0, "peak_rss_mb": 0, "cpu_time_ms": 0},
            "success": False,
            "logs_path": "runs/fast/scroll/2",
        },
    ]
    summary = _build_summary(runs)
    assert len(summary) == 1
    metrics = summary[0]["metrics"]
    # Summary rounds success_rate to 4 decimals — 2/3 = 0.6667.
    assert metrics["success_rate"] == 0.6667
    assert metrics["wall_clock_ms_p50"] in (100, 110)
    assert metrics["peak_rss_mb"] == 12


# ---------------------------------------------------------------------------
# end-to-end with mocked compose + fake runner
# ---------------------------------------------------------------------------


class _FakeRunner:
    """Stand-in used in unit tests; never instantiated by the orchestrator."""

    name = "example"
    image = _VALID_IMAGE

    def run(self, task: Task) -> RunResult:
        task.results_dir.mkdir(parents=True, exist_ok=True)
        (task.results_dir / "log.jsonl").write_text('{"fake": true}\n', encoding="utf-8")
        return RunResult(
            success=True,
            metrics=RunMetrics(wall_clock_ms=42, peak_rss_mb=10, cpu_time_ms=30),
            logs_path=task.results_dir,
        )


@pytest.fixture
def battle_with_fake_runner(tmp_path: Path) -> Path:
    """Scaffold a battle and replace the example runner with a no-docker fake."""
    target = init_battle("browsers", _GOOD_UUID, parent_dir=tmp_path)
    (target / "runners" / "example.py").write_text(
        textwrap.dedent(
            f"""
            from bench_kit.runner_base import Runner, RunMetrics, RunResult, Task
            from pathlib import Path

            class FakeRunner(Runner):
                name = "example"
                image = "{_VALID_IMAGE}"

                def run(self, task: Task) -> RunResult:
                    task.results_dir.mkdir(parents=True, exist_ok=True)
                    (task.results_dir / "log.jsonl").write_text('{{}}\\n', encoding="utf-8")
                    return RunResult(
                        success=True,
                        metrics=RunMetrics(wall_clock_ms=42, peak_rss_mb=10, cpu_time_ms=30),
                        logs_path=task.results_dir,
                    )
            """
        ).lstrip(),
        encoding="utf-8",
    )
    # Make the example task short — 2 iterations, no real timeout-relevance.
    task_path = target / "tasks" / "example.yaml"
    data = yaml.safe_load(task_path.read_text(encoding="utf-8"))
    data["iterations"] = 2
    data["timeout_s"] = 5
    task_path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return target


def _stub_compose(battle: Path) -> Any:
    """Patch out the compose lifecycle so tests don't touch docker."""
    return (
        patch("bench_kit.run._compose_up", return_value="fake-net"),
        patch("bench_kit.run._compose_down", return_value=None),
    )


def test_run_battle_emits_schema_valid_result(battle_with_fake_runner: Path) -> None:
    up, down = _stub_compose(battle_with_fake_runner)
    with up, down:
        out = run_battle(battle_with_fake_runner, RunOptions())
    assert out.is_file()
    data = json.loads(out.read_text(encoding="utf-8"))

    schema = load_schema("result")
    Draft202012Validator(schema).validate(data)

    # Two iterations of the example task on the example tool → 2 runs, 1 summary row.
    assert len(data["runs"]) == 2
    assert len(data["summary"]) == 1
    assert data["summary"][0]["tool"] == "example"
    assert data["summary"][0]["metrics"]["success_rate"] == 1.0


def test_run_battle_filters_by_tools(battle_with_fake_runner: Path) -> None:
    up, down = _stub_compose(battle_with_fake_runner)
    with up, down, pytest.raises(RunError, match="no runners matched"):
        run_battle(battle_with_fake_runner, RunOptions(tools=("nonexistent",)))


def test_run_battle_rejects_results_dir_outside_battle(
    battle_with_fake_runner: Path, tmp_path: Path
) -> None:
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    up, down = _stub_compose(battle_with_fake_runner)
    with up, down, pytest.raises(RunError, match="outside the battle directory"):
        run_battle(battle_with_fake_runner, RunOptions(results_dir=outside))


# ---------------------------------------------------------------------------
# expected-vs-output enforcement
# ---------------------------------------------------------------------------


def test_matches_expected_happy_path() -> None:
    ok, msg = _matches_expected({"count": 50, "title": "x"}, {"count": 50, "title": "x"})
    assert ok is True
    assert msg is None


def test_matches_expected_mismatch_explains() -> None:
    ok, msg = _matches_expected({"count": 51}, {"count": 50})
    assert ok is False
    assert msg is not None
    assert "count" in msg
    assert "50" in msg
    assert "51" in msg


def test_matches_expected_skips_placeholder_keys() -> None:
    """Keys present in expected but absent from output are skipped."""
    ok, msg = _matches_expected(
        {"count": 50},
        {"count": 50, "hash_present": True},  # hash_present is a placeholder
    )
    assert ok is True
    assert msg is None


def test_matches_expected_empty() -> None:
    ok, msg = _matches_expected({"count": 1}, {})
    assert ok is True
    assert msg is None


def test_matches_expected_deep() -> None:
    """Deep equality — nested dicts and lists compared structurally."""
    ok, _ = _matches_expected(
        {"items": [{"id": 1}, {"id": 2}]},
        {"items": [{"id": 1}, {"id": 2}]},
    )
    assert ok is True
    ok2, _ = _matches_expected(
        {"items": [{"id": 1}, {"id": 2}]},
        {"items": [{"id": 1}, {"id": 99}]},
    )
    assert ok2 is False


def test_run_battle_marks_mismatching_output_as_failed(
    battle_with_fake_runner: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A runner that exits 0 but writes wrong output.json is recorded as failed."""
    # Replace the fake runner with one that writes output.json with WRONG values
    # against the task's expected block (`count: 100` per the scaffold).
    target = battle_with_fake_runner
    (target / "runners" / "example.py").write_text(
        textwrap.dedent(
            f"""
            import json
            from bench_kit.runner_base import Runner, RunMetrics, RunResult, Task

            class FakeRunner(Runner):
                name = "example"
                image = "{_VALID_IMAGE}"

                def run(self, task: Task) -> RunResult:
                    task.results_dir.mkdir(parents=True, exist_ok=True)
                    (task.results_dir / "output.json").write_text(
                        json.dumps({{"count": 999}}),  # task expects 100
                        encoding="utf-8",
                    )
                    return RunResult(
                        success=True,
                        metrics=RunMetrics(wall_clock_ms=1, peak_rss_mb=1, cpu_time_ms=1),
                        logs_path=task.results_dir,
                    )
            """
        ).lstrip(),
        encoding="utf-8",
    )

    up, down = _stub_compose(target)
    with up, down:
        out = run_battle(target, RunOptions())
    data = json.loads(out.read_text(encoding="utf-8"))
    assert all(r["success"] is False for r in data["runs"])
    assert data["summary"][0]["metrics"]["success_rate"] == 0.0
    captured = capsys.readouterr()
    assert "expected.count=100" in captured.err


def test_run_battle_keeps_success_when_output_matches_expected(
    battle_with_fake_runner: Path,
) -> None:
    """A runner that writes output.json with values matching expected stays success."""
    target = battle_with_fake_runner
    (target / "runners" / "example.py").write_text(
        textwrap.dedent(
            f"""
            import json
            from bench_kit.runner_base import Runner, RunMetrics, RunResult, Task

            class FakeRunner(Runner):
                name = "example"
                image = "{_VALID_IMAGE}"

                def run(self, task: Task) -> RunResult:
                    task.results_dir.mkdir(parents=True, exist_ok=True)
                    (task.results_dir / "output.json").write_text(
                        json.dumps({{"count": 100}}),  # matches task.expected
                        encoding="utf-8",
                    )
                    return RunResult(
                        success=True,
                        metrics=RunMetrics(wall_clock_ms=1, peak_rss_mb=1, cpu_time_ms=1),
                        logs_path=task.results_dir,
                    )
            """
        ).lstrip(),
        encoding="utf-8",
    )

    up, down = _stub_compose(target)
    with up, down:
        out = run_battle(target, RunOptions())
    data = json.loads(out.read_text(encoding="utf-8"))
    assert all(r["success"] is True for r in data["runs"])
    assert data["summary"][0]["metrics"]["success_rate"] == 1.0


def test_run_battle_marks_malformed_output_json_as_failed(
    battle_with_fake_runner: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A runner that writes garbage to output.json must fail the iteration.

    Distinct from "no output.json at all" — that case keeps the runner's
    exit-code success for back-compat with legacy runners. Garbage is a
    runner bug we want to surface.
    """
    target = battle_with_fake_runner
    (target / "runners" / "example.py").write_text(
        textwrap.dedent(
            f"""
            from bench_kit.runner_base import Runner, RunMetrics, RunResult, Task

            class FakeRunner(Runner):
                name = "example"
                image = "{_VALID_IMAGE}"

                def run(self, task: Task) -> RunResult:
                    task.results_dir.mkdir(parents=True, exist_ok=True)
                    (task.results_dir / "output.json").write_text(
                        "this is not json",
                        encoding="utf-8",
                    )
                    return RunResult(
                        success=True,
                        metrics=RunMetrics(wall_clock_ms=1, peak_rss_mb=1, cpu_time_ms=1),
                        logs_path=task.results_dir,
                    )
            """
        ).lstrip(),
        encoding="utf-8",
    )

    up, down = _stub_compose(target)
    with up, down:
        out = run_battle(target, RunOptions())
    data = json.loads(out.read_text(encoding="utf-8"))
    assert all(r["success"] is False for r in data["runs"])
    captured = capsys.readouterr()
    assert "malformed" in captured.err


def test_run_battle_marks_non_dict_output_json_as_failed(
    battle_with_fake_runner: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """output.json must decode to an object, not an array or scalar."""
    target = battle_with_fake_runner
    (target / "runners" / "example.py").write_text(
        textwrap.dedent(
            f"""
            from bench_kit.runner_base import Runner, RunMetrics, RunResult, Task

            class FakeRunner(Runner):
                name = "example"
                image = "{_VALID_IMAGE}"

                def run(self, task: Task) -> RunResult:
                    task.results_dir.mkdir(parents=True, exist_ok=True)
                    (task.results_dir / "output.json").write_text(
                        "[1, 2, 3]",  # JSON-valid but not an object
                        encoding="utf-8",
                    )
                    return RunResult(
                        success=True,
                        metrics=RunMetrics(wall_clock_ms=1, peak_rss_mb=1, cpu_time_ms=1),
                        logs_path=task.results_dir,
                    )
            """
        ).lstrip(),
        encoding="utf-8",
    )

    up, down = _stub_compose(target)
    with up, down:
        out = run_battle(target, RunOptions())
    data = json.loads(out.read_text(encoding="utf-8"))
    assert all(r["success"] is False for r in data["runs"])
    captured = capsys.readouterr()
    assert "must be an object" in captured.err


def test_run_battle_no_output_json_falls_back_to_runner_success(
    battle_with_fake_runner: Path,
) -> None:
    """Runners that don't write output.json keep their reported success."""
    # The default fake runner in the fixture writes log.jsonl but no output.json.
    # That runner's RunResult.success=True must propagate unchanged.
    up, down = _stub_compose(battle_with_fake_runner)
    with up, down:
        out = run_battle(battle_with_fake_runner, RunOptions())
    data = json.loads(out.read_text(encoding="utf-8"))
    assert all(r["success"] is True for r in data["runs"])


def test_run_battle_fails_on_validation(tmp_path: Path) -> None:
    target = init_battle("browsers", _GOOD_UUID, parent_dir=tmp_path)
    (target / "meta.yaml").unlink()  # make validation fail
    up, down = _stub_compose(target)
    with up, down, pytest.raises(RunError, match="did not validate"):
        run_battle(target, RunOptions())
