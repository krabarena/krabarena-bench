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

from bench_kit.lint import _IMAGE_DIGEST_RE

_DOCKER = "docker"
_STATS_POLL_INTERVAL_S = 0.2
_KILL_GRACE_S = 1.0
_DEFAULT_PIDS_LIMIT = 256
_TMPFS_SPEC = "/tmp:size=512m,rw,nosuid,nodev"
_USER = "1000:1000"


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
    if not _IMAGE_DIGEST_RE.match(image):
        msg = (
            f"image {image!r} is not sha256-pinned; refusing to launch. "
            f"Use `<repo>@sha256:<64-hex>`."
        )
        raise ExecError(msg)
    if shutil.which(_DOCKER) is None:
        msg = "docker CLI not found on PATH; install Docker to run benchmarks"
        raise ExecError(msg)

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


def _docker_create(argv: list[str]) -> str:
    """Run ``docker create`` and return the resulting container id."""
    proc = subprocess.run(
        argv,
        capture_output=True,
        text=True,
        timeout=30,
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


def _docker_kill(cid: str) -> None:
    subprocess.run(
        [_DOCKER, "kill", cid],
        capture_output=True,
        timeout=10,
        check=False,
    )


def _docker_rm(cid: str) -> None:
    subprocess.run(
        [_DOCKER, "rm", "-f", cid],
        capture_output=True,
        timeout=10,
        check=False,
    )


# ---------------------------------------------------------------------------
# stats polling
# ---------------------------------------------------------------------------


def _poll_stats(
    cid: str,
    samples: list[dict[str, str]],
    stop: threading.Event,
) -> None:
    """Poll ``docker stats --no-stream`` until ``stop`` is set."""
    while not stop.is_set():
        try:
            proc = subprocess.run(
                [
                    _DOCKER,
                    "stats",
                    "--no-stream",
                    "--format",
                    "{{json .}}",
                    cid,
                ],
                capture_output=True,
                text=True,
                timeout=3,
                check=False,
            )
        except subprocess.TimeoutExpired:
            stop.wait(_STATS_POLL_INTERVAL_S)
            continue
        line = proc.stdout.strip()
        if proc.returncode == 0 and line:
            with contextlib.suppress(json.JSONDecodeError):
                samples.append(json.loads(line))
        stop.wait(_STATS_POLL_INTERVAL_S)


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


__all__ = [
    "ExecError",
    "ExecLimits",
    "ExecResult",
    "Mount",
    "run_constrained",
]
