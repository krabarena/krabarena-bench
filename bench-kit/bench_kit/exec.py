"""Sandboxed container exec — the only path bench-kit uses to run user code.

See ``SPEC.md`` §6 for the canonical sandbox policy. This module is the
single point at which Docker is invoked during a benchmark; runners
cannot bypass it because their AST is linted to forbid ``subprocess``,
``socket``, the ``docker`` SDK and friends. Anything that needs to
launch a container goes through :func:`run_constrained`, which applies
every flag the policy demands.

The implementation uses ``docker create`` + ``docker start --attach``
(rather than ``docker run``) so the container id is known *before* the
process starts; that lets us poll ``docker stats`` in a side thread for
peak-RSS and CPU-percent samples while the container runs.

Caveats — explicit so they don't bite later:

* ``cpu_time_ms`` is **approximated** as ``wall_clock_ms x avg_cpu_pct
  / 100``. Docker exposes per-container CPU% via ``docker stats``;
  exact CPU-time would require reading cgroup ``cpu.stat`` which is
  not portable across Docker Desktop / Linux hosts. For benchmark
  comparisons the approximation is enough; we document it in the
  spec.
* ``peak_rss_mb`` is the **max sample** across polls (default 200 ms
  cadence). Tasks much shorter than the cadence will undercount.
* On ``TimeoutExpired`` we ``docker kill`` (SIGKILL); we do not give
  the container a chance to exit gracefully — benchmarks should not
  rely on shutdown hooks.
"""

from __future__ import annotations

import contextlib
import json
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from bench_kit.lint import IMAGE_DIGEST_RE
from bench_kit.runner_base import RunMetrics, RunResult, Task

_DOCKER = "docker"
_KILL_GRACE_S = 1.0
_STATS_TERMINATE_GRACE_S = 2.0
_DEFAULT_PIDS_LIMIT = 256
_TMPFS_SPEC = "/tmp:size=512m,rw,nosuid,nodev"
_USER = "1000:1000"
# `docker stats` emits ANSI cursor-control sequences around each JSON
# line even with `--format` (it draws a "live" table by default).
# Strip them before json.loads.
_ANSI_RE = re.compile(r"\x1b\[[\d;]*[a-zA-Z]")


class ExecError(Exception):
    """Raised when the harness cannot prepare or invoke a container.

    Setup-time failures only — image-pin violation, ``docker`` CLI
    missing, ``docker create`` rejected. Runtime failures of the
    container itself are reported through :class:`ExecResult` with
    ``success=False``.
    """


@dataclass(frozen=True, slots=True)
class ExecLimits:
    """Resource caps applied to a single container run."""

    memory_mb: int
    cpus: float
    timeout_s: int
    pids: int = _DEFAULT_PIDS_LIMIT


@dataclass(frozen=True, slots=True)
class Mount:
    """A bind-mount from the host into the container.

    Anything bound is the runner's responsibility to make minimal —
    no ``$HOME``, no docker socket, no credential paths. The framework
    cannot enforce that statically beyond the ``runners/*.py`` lint;
    convention is that callers pass only the task's read-only
    ``/task`` and writable ``/results``.
    """

    host: Path
    container: str
    readonly: bool = False


@dataclass(frozen=True, slots=True)
class ExecResult:
    """Outcome of one constrained container run."""

    success: bool
    """``True`` iff the container exited 0 and was not timed out / OOM-killed."""

    exit_code: int
    timed_out: bool
    wall_clock_ms: int
    peak_rss_mb: int
    cpu_time_ms: int
    stdout: bytes
    stderr: bytes


def run_constrained(
    *,
    image: str,
    args: list[str],
    network: str,
    mounts: list[Mount],
    limits: ExecLimits,
    env: dict[str, str] | None = None,
) -> ExecResult:
    """Launch ``image`` with full sandbox flags applied; collect metrics.

    ``image`` must be sha256-pinned (``<repo>@sha256:<64-hex>``). The
    framework lint already enforces this on every runner module, but
    we re-check at exec time as defence in depth — a runner that
    bypassed the lint somehow still cannot launch a tag-based image.

    ``network`` is the docker network name to attach. Callers are
    responsible for ensuring it is one of the ``internal: true``
    fixture networks; ``bench validate`` blocks anything else at
    static-check time.
    """
    if not IMAGE_DIGEST_RE.match(image):
        msg = (
            f"image {image!r} is not sha256-pinned; refusing to launch. "
            f"Use `<repo>@sha256:<64-hex>`."
        )
        raise ExecError(msg)
    if shutil.which(_DOCKER) is None:
        msg = "docker CLI not found on PATH; install Docker to run benchmarks"
        raise ExecError(msg)

    _ensure_image_local(image)
    create_argv = _build_create_argv(image, args, network, mounts, limits, env)
    cid = _docker_create(create_argv)

    samples: list[dict[str, str]] = []
    stop = threading.Event()
    poller = threading.Thread(target=_poll_stats, args=(cid, samples, stop), daemon=True)
    poller.start()

    started_at = time.monotonic_ns()
    timed_out = False
    try:
        completed = subprocess.run(
            [_DOCKER, "start", "--attach", cid],
            capture_output=True,
            timeout=limits.timeout_s,
            check=False,
        )
        exit_code = completed.returncode
        stdout = completed.stdout
        stderr = completed.stderr
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        _docker_kill(cid)
        # Drain whatever we got before the kill.
        stdout = exc.stdout or b""
        stderr = exc.stderr or b""
        exit_code = 124  # convention: timeout
    finally:
        wall_clock_ms = (time.monotonic_ns() - started_at) // 1_000_000
        stop.set()
        poller.join(timeout=_KILL_GRACE_S)
        _docker_rm(cid)

    peak_rss_mb, avg_cpu_pct = _summarise_samples(samples)
    cpu_time_ms = int(wall_clock_ms * avg_cpu_pct / 100)
    return ExecResult(
        success=(exit_code == 0 and not timed_out),
        exit_code=exit_code,
        timed_out=timed_out,
        wall_clock_ms=int(wall_clock_ms),
        peak_rss_mb=peak_rss_mb,
        cpu_time_ms=cpu_time_ms,
        stdout=stdout,
        stderr=stderr,
    )


# ---------------------------------------------------------------------------
# argv construction
# ---------------------------------------------------------------------------


def _build_create_argv(
    image: str,
    args: list[str],
    network: str,
    mounts: list[Mount],
    limits: ExecLimits,
    env: dict[str, str] | None,
) -> list[str]:
    argv: list[str] = [_DOCKER, "create"]
    argv += [
        "--read-only",
        "--tmpfs",
        _TMPFS_SPEC,
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--user",
        _USER,
        "--pids-limit",
        str(limits.pids),
        "--memory",
        f"{limits.memory_mb}m",
        # memory-swap = memory disables swap accounting bypass.
        "--memory-swap",
        f"{limits.memory_mb}m",
        "--cpus",
        str(limits.cpus),
        "--network",
        network,
    ]
    for m in mounts:
        argv += ["--mount", _format_mount(m)]
    for key, value in (env or {}).items():
        argv += ["--env", f"{key}={value}"]
    argv += [image, *args]
    return argv


def _format_mount(m: Mount) -> str:
    parts = ["type=bind", f"src={m.host.resolve()}", f"dst={m.container}"]
    if m.readonly:
        parts.append("readonly")
    return ",".join(parts)


# ---------------------------------------------------------------------------
# docker invocations
# ---------------------------------------------------------------------------


_TIMEOUT_CREATE_S = 30.0
_TIMEOUT_KILL_S = 10.0
_TIMEOUT_RM_S = 10.0
_TIMEOUT_INSPECT_S = 10.0
# Pulls of multi-GB Playwright/Chromium images on a fresh CI runner
# can easily take a few minutes. Generous ceiling so the create
# timeout above stays tight for the in-cache path.
_TIMEOUT_PULL_S = 600.0


def _docker(args: list[str], *, timeout: float) -> subprocess.CompletedProcess[str]:
    """Single shape for every ``docker <subcommand>`` invocation in this module.

    Centralising the four call sites avoids drift between them and makes
    the timeout discipline visible at the top of the file.
    """
    return subprocess.run(
        [_DOCKER, *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _docker_create(argv: list[str]) -> str:
    """Run ``docker create`` and return the resulting container id.

    ``argv`` already includes ``docker create`` because it is built
    by :func:`_build_create_argv`; we shell out directly rather than
    through :func:`_docker` to preserve that.
    """
    proc = subprocess.run(
        argv,
        capture_output=True,
        text=True,
        timeout=_TIMEOUT_CREATE_S,
        check=False,
    )
    if proc.returncode != 0:
        msg = f"docker create failed (exit {proc.returncode}): {proc.stderr.strip()}"
        raise ExecError(msg)
    cid = proc.stdout.strip()
    if not cid:
        msg = "docker create produced no container id"
        raise ExecError(msg)
    return cid


def _ensure_image_local(image: str) -> None:
    """Pull ``image`` if it isn't already in the local Docker store.

    Without this, ``docker create`` does the pull implicitly inside
    its own short timeout — which the multi-GB Playwright + Chromium
    images cannot complete on a cold-cache CI runner. Running an
    explicit pull with a generous ceiling ahead of create keeps the
    in-cache fast path tight and the cold-cache path slow-but-honest.
    """
    inspect = _docker(["image", "inspect", image], timeout=_TIMEOUT_INSPECT_S)
    if inspect.returncode == 0:
        return
    pull = _docker(["pull", "--quiet", image], timeout=_TIMEOUT_PULL_S)
    if pull.returncode != 0:
        msg = (
            f"docker pull {image} failed (exit {pull.returncode}): "
            f"{pull.stderr.strip() or pull.stdout.strip()}"
        )
        raise ExecError(msg)


def _docker_kill(cid: str) -> None:
    _docker(["kill", cid], timeout=_TIMEOUT_KILL_S)


def _docker_rm(cid: str) -> None:
    _docker(["rm", "-f", cid], timeout=_TIMEOUT_RM_S)


# ---------------------------------------------------------------------------
# stats streaming
# ---------------------------------------------------------------------------


def _poll_stats(
    cid: str,
    samples: list[dict[str, str]],
    stop: threading.Event,
) -> None:
    """Stream ``docker stats <cid>`` line-by-line until ``stop`` is set.

    Replaces the older `--no-stream` polling loop, which on macOS spent
    ~50-100 ms per fork — long enough that sub-1s containers produced
    zero samples and `peak_rss_mb` was always 0 in the result. Using
    one streaming Popen, samples land in the list as fast as Docker
    emits them; a watcher thread terminates the process when the
    caller signals stop, so the main reader exits naturally on EOF.
    """
    try:
        proc = subprocess.Popen(
            [_DOCKER, "stats", "--format", "{{json .}}", cid],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
    except (FileNotFoundError, OSError):
        return
    if proc.stdout is None:
        proc.terminate()
        return

    def _terminate_when_stopped() -> None:
        stop.wait()
        with contextlib.suppress(ProcessLookupError):
            proc.terminate()

    watcher = threading.Thread(target=_terminate_when_stopped, daemon=True)
    watcher.start()

    try:
        for line in proc.stdout:
            stripped = _ANSI_RE.sub("", line).strip()
            if not stripped:
                continue
            with contextlib.suppress(json.JSONDecodeError):
                samples.append(json.loads(stripped))
    finally:
        with contextlib.suppress(ProcessLookupError):
            proc.terminate()
        try:
            proc.wait(timeout=_STATS_TERMINATE_GRACE_S)
        except subprocess.TimeoutExpired:
            proc.kill()
            with contextlib.suppress(subprocess.TimeoutExpired):
                proc.wait(timeout=1.0)


def _summarise_samples(samples: list[dict[str, str]]) -> tuple[int, float]:
    """Return (peak_rss_mb, avg_cpu_pct) over collected samples."""
    if not samples:
        return 0, 0.0
    peak = 0
    cpu_sum = 0.0
    cpu_n = 0
    for s in samples:
        mem = _parse_mb(s.get("MemUsage", ""))
        peak = max(peak, mem)
        cpu = _parse_pct(s.get("CPUPerc", ""))
        if cpu is not None:
            cpu_sum += cpu
            cpu_n += 1
    avg_cpu = cpu_sum / cpu_n if cpu_n else 0.0
    return peak, avg_cpu


_SIZE_RE = re.compile(r"^\s*([\d.]+)\s*([KMGT])iB", flags=re.IGNORECASE)
_BYTES_RE = re.compile(r"^\s*([\d.]+)\s*B", flags=re.IGNORECASE)


def _parse_mb(usage: str) -> int:
    """Parse the leading ``X.YMiB`` half of ``"3.5MiB / 4GiB"``."""
    head = usage.split("/", 1)[0]
    m = _SIZE_RE.match(head)
    if m:
        val = float(m.group(1))
        unit = m.group(2).upper()
        if unit == "K":
            return int(val / 1024)
        if unit == "M":
            return int(val)
        if unit == "G":
            return int(val * 1024)
        if unit == "T":
            return int(val * 1024 * 1024)
    b = _BYTES_RE.match(head)
    if b:
        return int(float(b.group(1)) / (1024 * 1024))
    return 0


def _parse_pct(s: str) -> float | None:
    if not s:
        return None
    s = s.strip().rstrip("%")
    try:
        return float(s)
    except ValueError:
        return None


def run_task_in_sandbox(
    task: Task,
    *,
    image: str,
    args: list[str],
    extra_env: dict[str, str] | None = None,
) -> RunResult:
    """High-level helper: run ``image`` against ``task``, write the log,
    and return a :class:`bench_kit.runner_base.RunResult`.

    This is the recommended entrypoint for runners. It encapsulates the
    full mounting / limits / log-write boilerplate so a real runner can
    be ten lines:

    .. code-block:: python

        def run(self, task: Task) -> RunResult:
            return run_task_in_sandbox(
                task,
                image=self.image,
                args=["node", "/task/driver.js", task.id],
            )

    The log file is JSONL at ``<task.results_dir>/log.jsonl`` with one
    record carrying exit code, timeout flag, stdout, stderr, and the
    metric samples. The returned ``RunResult.logs_path`` is
    ``task.results_dir`` itself — the **directory** containing the
    log, plus any artefacts a runner might add later (screenshots,
    traces). The orchestrator records that directory in
    ``result.json``'s ``logs_path`` field, matching the schema's
    ``runs/...`` shape.
    """
    result = run_constrained(
        image=image,
        args=args,
        network=task.fixture_network,
        mounts=[
            Mount(host=task.dir, container="/task", readonly=True),
            Mount(host=task.results_dir, container="/results"),
        ],
        limits=ExecLimits(
            memory_mb=task.limits.memory_mb,
            cpus=task.limits.cpus,
            timeout_s=task.timeout_s,
        ),
        env=extra_env,
    )

    log_path = task.results_dir / "log.jsonl"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "task_id": task.id,
        "exit_code": result.exit_code,
        "timed_out": result.timed_out,
        "wall_clock_ms": result.wall_clock_ms,
        "peak_rss_mb": result.peak_rss_mb,
        "cpu_time_ms": result.cpu_time_ms,
        "stdout": result.stdout.decode("utf-8", errors="replace"),
        "stderr": result.stderr.decode("utf-8", errors="replace"),
    }
    log_path.write_text(json.dumps(record, ensure_ascii=False) + "\n", encoding="utf-8")

    return RunResult(
        success=result.success,
        metrics=RunMetrics(
            wall_clock_ms=result.wall_clock_ms,
            peak_rss_mb=result.peak_rss_mb,
            cpu_time_ms=result.cpu_time_ms,
        ),
        logs_path=task.results_dir,
    )


__all__ = [
    "ExecError",
    "ExecLimits",
    "ExecResult",
    "Mount",
    "run_constrained",
    "run_task_in_sandbox",
]
