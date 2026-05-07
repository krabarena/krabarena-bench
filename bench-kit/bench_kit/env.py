"""Auto-collected ``env`` block for ``result.json``.

The shape is fixed by ``result.schema.json`` and reproduced here. Values
are gathered at run time from ``/proc``, ``uname``, ``docker info`` and
the bench-kit package itself; **callers cannot override them** — see
SPEC §3.2 for the rationale (anti-mistake, not anti-fraud, but the
provenance must be machine-collected so manual edits are detectable).
"""

from __future__ import annotations

import hashlib
import os
import platform
import subprocess
from pathlib import Path
from typing import Any

from bench_kit import __version__ as _BENCH_KIT_VERSION

_BENCH_KIT_PACKAGE_DIR = Path(__file__).parent


def collect_env() -> dict[str, Any]:
    """Return a fresh ``env`` block conforming to ``result.schema.json`` §Env."""
    uname = platform.uname()
    return {
        "os": f"{uname.system} {uname.release}",
        "arch": uname.machine,
        "cpu_model": _cpu_model(),
        "cpu_count": os.cpu_count() or 1,
        "mem_total_mb": _mem_total_mb(),
        "docker_version": _docker_version(),
        "kernel_version": uname.version,
        "bench_kit_version": _BENCH_KIT_VERSION,
        "runtime_hash": _runtime_hash(),
        "hostname_redacted": True,
    }


def _cpu_model() -> str:
    """Best-effort CPU brand string across Linux and macOS."""
    cpuinfo = Path("/proc/cpuinfo")
    if cpuinfo.is_file():
        for line in cpuinfo.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith("model name"):
                _, _, value = line.partition(":")
                return value.strip()
    try:
        proc = subprocess.run(
            ["sysctl", "-n", "machdep.cpu.brand_string"],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except (FileNotFoundError, subprocess.SubprocessError):
        return "unknown"
    out = proc.stdout.strip()
    return out or "unknown"


def _mem_total_mb() -> int:
    """Total physical RAM in MiB, integer.

    Falls back to ``1`` (not ``0``) when neither ``/proc/meminfo`` nor
    ``sysctl hw.memsize`` is available, because the result schema
    requires ``mem_total_mb >= 1``. A nonsense-but-valid value keeps
    `bench run` going on niche hosts; the bundle will record it as
    `1` rather than failing schema validation.
    """
    meminfo = Path("/proc/meminfo")
    if meminfo.is_file():
        for line in meminfo.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith("MemTotal:"):
                parts = line.split()
                if len(parts) >= 2 and parts[1].isdigit():
                    # MemTotal is in kB.
                    return max(int(parts[1]) // 1024, 1)
    try:
        proc = subprocess.run(
            ["sysctl", "-n", "hw.memsize"],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except (FileNotFoundError, subprocess.SubprocessError):
        return 1
    out = proc.stdout.strip()
    if out.isdigit():
        return max(int(out) // (1024 * 1024), 1)
    return 1


def _docker_version() -> str:
    try:
        proc = subprocess.run(
            ["docker", "version", "--format", "{{.Server.Version}}"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (FileNotFoundError, subprocess.SubprocessError):
        return "unknown"
    out = proc.stdout.strip()
    return out or "unknown"


def _runtime_hash() -> str:
    """sha256 of the bench-kit Python sources actually loaded.

    Defends against the case where someone hand-patches their local
    bench-kit and the rest of the bundle silently lies about which
    code was running.
    """
    h = hashlib.sha256()
    for path in sorted(_BENCH_KIT_PACKAGE_DIR.rglob("*.py")):
        h.update(path.relative_to(_BENCH_KIT_PACKAGE_DIR).as_posix().encode())
        h.update(b"\0")
        h.update(path.read_bytes())
    for path in sorted(_BENCH_KIT_PACKAGE_DIR.rglob("*.json")):
        h.update(path.relative_to(_BENCH_KIT_PACKAGE_DIR).as_posix().encode())
        h.update(b"\0")
        h.update(path.read_bytes())
    return f"sha256:{h.hexdigest()}"


__all__ = ["collect_env"]
