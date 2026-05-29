"""Bench orchestrator — produces ``result.json`` for one battle.

This is the meat of ``bench run``. It pre-validates the battle, brings
up the Compose fixtures on a private project name, dynamically imports
each runner module, executes every (tool, task, iteration) tuple, and
emits a ``result.json`` conforming to the schema.

Threats from buggy or merely-wrong runners are bounded by:

* ``validate_battle`` rejecting forbidden imports / unpinned images
  before we ever execute a runner.
* ``run_constrained`` forcing every container launch through a
  policy-applying chokepoint.
* Compose project name carrying a random suffix so concurrent benches
  on the same host can't collide on networks or service names.

Anything more ambitious (full process isolation of the runner Python
itself) is explicitly out of scope: runners are reviewed code on the
``main`` branch of ``krabarena-bench``, not third-party uploads.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from bench_kit import SPEC_VERSION
from bench_kit.env import collect_env
from bench_kit.runner_base import Runner, Task, TaskLimits
from bench_kit.validate import validate_battle


class RunError(Exception):
    """Raised when the orchestrator cannot proceed.

    Bench-time failures (validation, compose start, runner discovery,
    git state). Per-iteration failures of the container itself are
    recorded as ``success=False`` in ``result.json`` and never raise.
    """


@dataclass(frozen=True, slots=True)
class RunOptions:
    tools: tuple[str, ...] | None = None
    """If set, only these tool names are executed."""

    results_dir: Path | None = None
    """Override results root; defaults to ``<battle_dir>/results``."""

    battle_repo: str = "github.com/krabarena/krabarena-bench"
    """Canonical repo slug recorded in ``result.json``."""


def run_battle(battle_dir: Path, options: RunOptions | None = None) -> Path:
    """Execute a battle and return the path to its ``result.json``."""
    opts = options or RunOptions()
    battle_dir = battle_dir.resolve()

    report = validate_battle(battle_dir)
    if not report.ok:
        msg = f"battle did not validate: {len(report.issues)} issue(s); fix with `bench validate`"
        raise RunError(msg)

    meta = _read_yaml(battle_dir / "meta.yaml")
    tasks = _load_tasks(battle_dir / str(meta.get("tasks_dir", "tasks")))
    runners = _load_runners(battle_dir / str(meta.get("runners_dir", "runners")))
    if opts.tools:
        runners = {name: cls for name, cls in runners.items() if name in set(opts.tools)}
        if not runners:
            msg = f"no runners matched --tools={opts.tools!r}"
            raise RunError(msg)

    results_dir = _resolve_results_dir(battle_dir, opts.results_dir)
    if results_dir.exists():
        shutil.rmtree(results_dir)
    results_dir.mkdir(parents=True)

    project = f"krabbench-{secrets.token_hex(4)}"
    compose_path = battle_dir / str(meta["fixtures"]["compose"])
    network_name = _compose_up(compose_path, project)

    runs: list[dict[str, Any]] = []
    ran_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    try:
        for tool_name, runner_cls in runners.items():
            runner = runner_cls()
            for task_id, task_data in tasks.items():
                _execute_task(
                    runner=runner,
                    tool_name=tool_name,
                    task_id=task_id,
                    task_data=task_data,
                    battle_dir=battle_dir,
                    results_dir=results_dir,
                    fixture_network=network_name,
                    runs_out=runs,
                )
    finally:
        _compose_down(compose_path, project)

    summary = _build_summary(runs)
    tools_block = _build_tools_block(runners, battle_dir)

    result: dict[str, Any] = {
        "schema_version": SPEC_VERSION,
        "battle_id": meta["battle_id"],
        "battle_repo": opts.battle_repo,
        "battle_commit": _git_rev_parse_head(battle_dir),
        "ran_at": ran_at,
        "env": collect_env(),
        "tools": tools_block,
        "runs": runs,
        "summary": summary,
    }

    out = results_dir / "result.json"
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return out


def _resolve_results_dir(battle_dir: Path, override: Path | None) -> Path:
    """Resolve the results root, refusing values outside ``battle_dir``.

    The orchestrator wipes whatever ``results_dir`` points at before
    every run; without containment, ``--results-dir /`` would call
    ``shutil.rmtree("/")``. Force the override to be a path under
    ``battle_dir`` (or use the canonical ``<battle_dir>/results`` if
    none was given).
    """
    if override is None:
        return (battle_dir / "results").resolve()
    candidate = override.resolve()
    if candidate == battle_dir or not candidate.is_relative_to(battle_dir):
        msg = (
            f"--results-dir {override} resolves outside the battle directory; "
            f"refusing to wipe arbitrary paths"
        )
        raise RunError(msg)
    return candidate


# ---------------------------------------------------------------------------
# task / runner discovery
# ---------------------------------------------------------------------------


def _read_yaml(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict):
        msg = f"{path} must be a YAML mapping"
        raise RunError(msg)
    return data


def _load_tasks(tasks_dir: Path) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for path in sorted(p for p in tasks_dir.glob("*.yaml") if not p.name.startswith("_")):
        data = _read_yaml(path)
        out[data["id"]] = data
    if not out:
        msg = f"no tasks found in {tasks_dir}"
        raise RunError(msg)
    return out


def _load_runners(runners_dir: Path) -> dict[str, type[Runner]]:
    """Dynamic-import every ``runners/<tool>.py`` and return ``{name: cls}``.

    Each module must contain exactly one concrete :class:`Runner`
    subclass. Validation has already linted these files for forbidden
    imports; we trust them by virtue of being on the ``main`` branch.
    """
    out: dict[str, type[Runner]] = {}
    for path in sorted(p for p in runners_dir.glob("*.py") if not p.name.startswith("_")):
        module_name = f"_bench_runner_{path.stem}"
        spec = importlib.util.spec_from_file_location(module_name, path)
        if spec is None or spec.loader is None:
            msg = f"could not load runner spec for {path}"
            raise RunError(msg)
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        try:
            spec.loader.exec_module(module)
        except Exception as exc:
            msg = f"runner {path.name} failed to import: {exc!r}"
            raise RunError(msg) from exc
        cls = _find_runner_class(module, path)
        instance_name = getattr(cls, "name", None)
        if not isinstance(instance_name, str) or not instance_name:
            msg = f"runner {path.name}: class {cls.__name__} must set a string `name` attribute"
            raise RunError(msg)
        if instance_name in out:
            msg = f"duplicate runner name {instance_name!r}"
            raise RunError(msg)
        out[instance_name] = cls
    if not out:
        msg = f"no runners found in {runners_dir}"
        raise RunError(msg)
    return out


def _find_runner_class(module: object, path: Path) -> type[Runner]:
    candidates: list[type[Runner]] = []
    for attr in vars(module).values():
        if isinstance(attr, type) and attr is not Runner and issubclass(attr, Runner):
            candidates.append(attr)
    if not candidates:
        msg = f"runner {path.name}: no Runner subclass at module top level"
        raise RunError(msg)
    if len(candidates) > 1:
        names = ", ".join(c.__name__ for c in candidates)
        msg = f"runner {path.name}: more than one Runner subclass found ({names})"
        raise RunError(msg)
    return candidates[0]


# ---------------------------------------------------------------------------
# compose lifecycle
# ---------------------------------------------------------------------------


def _compose_up(compose_path: Path, project: str) -> str:
    # 300s budget (was 120s) so battles with several heavyweight
    # services — e.g. the browsers Battle, where Selenium 4 +
    # Chromium + a Browserless + Lightpanda + chromedp + nginx +
    # the app fixture all need to be Healthy before any task runs
    # — actually fit. 120s was a tight cap that worked for 2-3
    # services, broke once the battle grew to 6.
    proc = subprocess.run(
        [
            "docker",
            "compose",
            "-f",
            str(compose_path),
            "--project-name",
            project,
            "up",
            "-d",
            "--wait",
        ],
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    if proc.returncode != 0:
        msg = f"docker compose up failed: {proc.stderr.strip() or proc.stdout.strip()}"
        raise RunError(msg)
    return _discover_internal_network(compose_path, project)


def _compose_down(compose_path: Path, project: str) -> None:
    subprocess.run(
        [
            "docker",
            "compose",
            "-f",
            str(compose_path),
            "--project-name",
            project,
            "down",
            "-v",
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def _discover_internal_network(compose_path: Path, project: str) -> str:
    """Find the first ``internal: true`` network in compose and return
    its docker-managed full name (``<project>_<name>``)."""
    compose = _read_yaml(compose_path)
    networks = compose.get("networks") or {}
    for name, cfg in networks.items():
        if isinstance(cfg, dict) and cfg.get("internal", False):
            return f"{project}_{name}"
    msg = "compose file has no `internal: true` network — should have been caught by validate"
    raise RunError(msg)


# ---------------------------------------------------------------------------
# per-iteration execution
# ---------------------------------------------------------------------------


def _execute_task(
    *,
    runner: Runner,
    tool_name: str,
    task_id: str,
    task_data: dict[str, Any],
    battle_dir: Path,
    results_dir: Path,
    fixture_network: str,
    runs_out: list[dict[str, Any]],
) -> None:
    iterations = int(task_data["iterations"])
    expected = dict(task_data.get("expected") or {})
    for iteration in range(iterations):
        iter_results = results_dir / "runs" / tool_name / task_id / str(iteration)
        iter_results.mkdir(parents=True, exist_ok=True)
        task = _build_task(task_data, battle_dir, iter_results, fixture_network)
        run_res = runner.run(task)
        success = _final_success(
            run_res.success, iter_results, expected, tool_name, task_id, iteration
        )
        rel = iter_results.relative_to(results_dir).as_posix()
        runs_out.append(
            {
                "tool": tool_name,
                "task": task_id,
                "iteration": iteration,
                "metrics": {
                    "wall_clock_ms": run_res.metrics.wall_clock_ms,
                    "peak_rss_mb": run_res.metrics.peak_rss_mb,
                    "cpu_time_ms": run_res.metrics.cpu_time_ms,
                    **dict(run_res.metrics.extra),
                },
                "success": success,
                "logs_path": rel,
            }
        )


class _OutputJsonError(Exception):
    """Raised when ``output.json`` exists but cannot be parsed as a JSON object.

    Distinguished from the "file absent" path (which returns ``None``
    from :func:`_read_output_json`): a runner that wrote a malformed
    ``output.json`` is buggy, not legacy, and silently falling back
    to its exit-code success would mask the bug. See SPEC §3.4.
    """


def _final_success(
    runner_success: bool,
    iter_results: Path,
    expected: dict[str, Any],
    tool: str,
    task_id: str,
    iteration: int,
) -> bool:
    """Combine the runner's exit-code success with output-vs-expected check.

    The runner's container exit code is the first signal. If that's
    already false we keep it false. If true, we look for
    ``<iter_results>/output.json`` and assert every key in
    ``expected`` deep-equals the corresponding key in ``output``.
    Missing keys in output (placeholders like ``hash_present: true``
    waiting on a golden value) are skipped — see SPEC §3.4.

    A runner that doesn't write ``output.json`` at all keeps
    ``runner_success`` as the source of truth, so old battles and
    runners that pre-date the structured-output convention continue
    to work. A runner that writes a *malformed* ``output.json`` is
    a different case — the run fails with a diagnostic, because that
    is a runner bug we want to surface, not back-compat with a missing
    feature.
    """
    if not runner_success or not expected:
        return runner_success
    try:
        output = _read_output_json(iter_results)
    except _OutputJsonError as exc:
        print(
            f"task {task_id} iter {iteration}/{tool}: malformed output.json: {exc}",
            file=sys.stderr,
        )
        return False
    if output is None:
        return runner_success
    ok, mismatch = _matches_expected(output, expected)
    if not ok:
        print(
            f"task {task_id} iter {iteration}/{tool}: {mismatch}",
            file=sys.stderr,
        )
        return False
    return True


def _read_output_json(iter_results: Path) -> dict[str, Any] | None:
    """Parse ``<iter_results>/output.json``.

    Returns ``None`` only when the file does not exist (legacy
    runners). Raises :class:`_OutputJsonError` if the file is present
    but unreadable, isn't valid JSON, or doesn't decode to a JSON
    object — those are runner bugs that should fail the run loudly,
    not be silently absorbed into the exit-code path.
    """
    path = iter_results / "output.json"
    if not path.is_file():
        return None
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        msg = f"could not read {path.name}: {exc}"
        raise _OutputJsonError(msg) from exc
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        msg = f"{path.name} is not valid JSON: {exc}"
        raise _OutputJsonError(msg) from exc
    if not isinstance(data, dict):
        msg = f"{path.name} root must be an object, got {type(data).__name__}"
        raise _OutputJsonError(msg)
    return data


def _matches_expected(
    output: dict[str, Any],
    expected: dict[str, Any],
) -> tuple[bool, str | None]:
    """Deep-equal every key in ``expected`` against ``output``.

    Keys present in ``expected`` but absent from ``output`` are
    skipped — that's the placeholder convention for values pinned
    later (e.g. ``hash_present: true`` on a canvas hash before a
    verified Browserless run produces the golden). Returns
    ``(ok, mismatch_message)`` so the caller can surface a clear
    diagnostic when ``ok`` is false.
    """
    for key, want in expected.items():
        if key not in output:
            continue
        got = output[key]
        if got != want:
            return False, f"expected.{key}={want!r} but output.{key}={got!r}"
    return True, None


def _build_task(
    data: dict[str, Any],
    battle_dir: Path,
    results_dir: Path,
    fixture_network: str,
) -> Task:
    limits = data["limits"]
    return Task(
        id=data["id"],
        title=data["title"],
        description=data["description"],
        fixture_service=data["fixture_service"],
        inputs=dict(data.get("inputs") or {}),
        expected=dict(data.get("expected") or {}),
        success_predicate=data["success_predicate"],
        timeout_s=int(data["timeout_s"]),
        iterations=int(data["iterations"]),
        limits=TaskLimits(memory_mb=int(limits["memory_mb"]), cpus=float(limits["cpus"])),
        dir=battle_dir,
        results_dir=results_dir,
        fixture_network=fixture_network,
    )


# ---------------------------------------------------------------------------
# summary + tools block
# ---------------------------------------------------------------------------


def _build_summary(runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Aggregate per (tool, task) over iterations."""
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for r in runs:
        grouped.setdefault((r["tool"], r["task"]), []).append(r)

    out: list[dict[str, Any]] = []
    for (tool, task), rs in sorted(grouped.items()):
        successes = [r for r in rs if r["success"]]
        success_rate = len(successes) / len(rs) if rs else 0.0
        wall = sorted(r["metrics"]["wall_clock_ms"] for r in successes)
        rss = [r["metrics"]["peak_rss_mb"] for r in successes]
        cpu = sorted(r["metrics"]["cpu_time_ms"] for r in successes)
        metrics: dict[str, Any] = {"success_rate": round(success_rate, 4)}
        if wall:
            metrics["wall_clock_ms_p50"] = _percentile(wall, 50)
            metrics["wall_clock_ms_p95"] = _percentile(wall, 95)
            metrics["wall_clock_ms_p99"] = _percentile(wall, 99)
        if rss:
            metrics["peak_rss_mb"] = max(rss)
        if cpu:
            metrics["cpu_time_ms_p50"] = _percentile(cpu, 50)
        out.append({"tool": tool, "task": task, "metrics": metrics})
    return out


def _percentile(sorted_values: list[int], p: int) -> int:
    """Nearest-rank percentile, integer output to match the schema."""
    if not sorted_values:
        return 0
    if len(sorted_values) == 1:
        return sorted_values[0]
    rank = max(1, (p * len(sorted_values) + 99) // 100)
    return sorted_values[min(rank - 1, len(sorted_values) - 1)]


def _build_tools_block(
    runners: dict[str, type[Runner]],
    battle_dir: Path,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for name, cls in runners.items():
        image = getattr(cls, "image", "")
        digest = image.split("@", 1)[1] if "@" in image else ""
        runner_path = _runner_path_for(cls, battle_dir)
        out.append(
            {
                "name": name,
                "image_digest": digest,
                "version": _tool_version(image),
                "runner_path": runner_path,
            }
        )
    return out


def _runner_path_for(cls: type[Runner], battle_dir: Path) -> str:
    """Best-effort relative path to the runner module file."""
    module_file = sys.modules.get(cls.__module__, None)
    file = getattr(module_file, "__file__", None) if module_file else None
    if not file:
        return f"runners/{cls.name}.py"
    try:
        return Path(file).resolve().relative_to(battle_dir).as_posix()
    except ValueError:
        return f"runners/{cls.name}.py"


def _tool_version(image: str) -> str:
    """Parse the human-readable tag-or-repo portion before ``@``."""
    if "@" in image:
        return image.split("@", 1)[0]
    return image or "unknown"


# ---------------------------------------------------------------------------
# git
# ---------------------------------------------------------------------------


_GIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


def _git_rev_parse_head(path: Path) -> str:
    proc = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    out = proc.stdout.strip()
    if proc.returncode == 0 and _GIT_SHA_RE.match(out):
        return out
    # Tests and ad-hoc runs often have no git context; emit a synthetic
    # 40-char zero SHA so the schema still passes. `bench package` will
    # later refuse to build a Claim bundle from such a result, since the
    # commit cannot be cloned by a verifier. ``GIT_COMMIT`` honoured for
    # CI environments that resolve the SHA out-of-band, but only when it
    # matches the schema's strict 40-lowercase-hex shape.
    override = os.environ.get("GIT_COMMIT", "").strip()
    if _GIT_SHA_RE.match(override):
        return override
    return "0" * 40


__all__ = ["RunError", "RunOptions", "run_battle"]
