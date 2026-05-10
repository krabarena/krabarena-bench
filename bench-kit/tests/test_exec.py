"""Tests for the constrained-exec helper.

Most tests are mock-based — they exercise argv construction, image-pin
enforcement, timeout handling, and metric summarisation without ever
calling Docker. One end-to-end test is gated on a real Docker daemon
via the ``docker`` mark; CI runs it on Linux runners that have Docker
preinstalled, and it pulls a single tiny digest-pinned image.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from bench_kit.exec import (
    ExecError,
    ExecLimits,
    Mount,
    _build_create_argv,
    _parse_mb,
    _parse_pct,
    _summarise_samples,
    run_constrained,
)

_DIGEST = "lib/img@sha256:" + "0" * 64
_LIMITS = ExecLimits(memory_mb=256, cpus=1.0, timeout_s=10)


# ---------------------------------------------------------------------------
# argv construction
# ---------------------------------------------------------------------------


def test_argv_includes_all_sandbox_flags() -> None:
    argv = _build_create_argv(_DIGEST, ["echo", "hi"], "fixtures-net", [], _LIMITS, None)
    joined = " ".join(argv)
    for flag in (
        "--read-only",
        "--cap-drop ALL",
        "--security-opt no-new-privileges",
        "--user 1000:1000",
        "--pids-limit 256",
        "--memory 256m",
        "--memory-swap 256m",
        "--cpus 1.0",
        "--network fixtures-net",
        "--tmpfs /tmp:size=512m,rw,nosuid,nodev",
    ):
        assert flag in joined, f"missing flag: {flag!r}"


def test_argv_appends_image_and_args_last() -> None:
    argv = _build_create_argv(_DIGEST, ["echo", "hi"], "n", [], _LIMITS, None)
    assert argv[-3] == _DIGEST
    assert argv[-2:] == ["echo", "hi"]


def test_argv_includes_mounts(tmp_path: Path) -> None:
    mounts = [
        Mount(host=tmp_path / "task", container="/task", readonly=True),
        Mount(host=tmp_path / "results", container="/results", readonly=False),
    ]
    (tmp_path / "task").mkdir()
    (tmp_path / "results").mkdir()
    argv = _build_create_argv(_DIGEST, [], "n", mounts, _LIMITS, None)
    mount_args = [argv[i + 1] for i, x in enumerate(argv) if x == "--mount"]
    assert any("dst=/task" in m and "readonly" in m for m in mount_args)
    assert any("dst=/results" in m and "readonly" not in m for m in mount_args)


def test_argv_includes_env() -> None:
    argv = _build_create_argv(_DIGEST, [], "n", [], _LIMITS, {"FOO": "bar", "X": "1"})
    env_args = [argv[i + 1] for i, x in enumerate(argv) if x == "--env"]
    assert "FOO=bar" in env_args
    assert "X=1" in env_args


# ---------------------------------------------------------------------------
# image-pin defence-in-depth
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_image",
    [
        "lib/img:latest",
        "lib/img",
        "lib/img@sha256:short",
        "lib/img@md5:" + "0" * 32,
        "",
    ],
)
def test_run_rejects_unpinned_image(bad_image: str) -> None:
    with pytest.raises(ExecError, match="not sha256-pinned"):
        run_constrained(
            image=bad_image,
            args=[],
            network="n",
            mounts=[],
            limits=_LIMITS,
        )


# ---------------------------------------------------------------------------
# missing docker
# ---------------------------------------------------------------------------


def test_raises_when_docker_missing() -> None:
    with (
        patch("bench_kit.exec.shutil.which", return_value=None),
        pytest.raises(ExecError, match="docker CLI not found"),
    ):
        run_constrained(
            image=_DIGEST,
            args=[],
            network="n",
            mounts=[],
            limits=_LIMITS,
        )


# ---------------------------------------------------------------------------
# metric parsing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("usage", "expected"),
    [
        ("3.5MiB / 4GiB", 3),
        ("100KiB / 4GiB", 0),
        ("1.5GiB / 4GiB", 1536),
        ("2TiB / 8TiB", 2 * 1024 * 1024),
        ("12B / 4GiB", 0),
        ("", 0),
        ("not parseable", 0),
    ],
)
def test_parse_mb(usage: str, expected: int) -> None:
    assert _parse_mb(usage) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("12.34%", 12.34),
        ("0%", 0.0),
        ("200.0%", 200.0),
        ("", None),
        ("garbage", None),
    ],
)
def test_parse_pct(raw: str, expected: float | None) -> None:
    assert _parse_pct(raw) == expected


def test_summarise_samples_empty() -> None:
    assert _summarise_samples([]) == (0, 0.0)


def test_summarise_samples_takes_max_mem_avg_cpu() -> None:
    samples: list[dict[str, str]] = [
        {"MemUsage": "10MiB / 4GiB", "CPUPerc": "10%"},
        {"MemUsage": "30MiB / 4GiB", "CPUPerc": "30%"},
        {"MemUsage": "20MiB / 4GiB", "CPUPerc": "20%"},
    ]
    peak, avg = _summarise_samples(samples)
    assert peak == 30
    assert avg == pytest.approx(20.0)


# ---------------------------------------------------------------------------
# orchestration via mocks
# ---------------------------------------------------------------------------


class _FakeCompleted:
    def __init__(self, returncode: int, stdout: bytes = b"", stderr: bytes = b"") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _fake_run_factory(
    create_cid: str = "abc123",
    attach_returncode: int = 0,
    timeout: bool = False,
) -> Any:
    """Build a fake ``subprocess.run`` that simulates create + start + rm."""

    def fake_run(argv: list[str], **kwargs: Any) -> Any:
        cmd = argv[1] if len(argv) > 1 else ""
        if cmd == "create":
            return _FakeCompleted(0, stdout=f"{create_cid}\n".encode(), stderr=b"")
        if cmd == "start":
            if timeout:
                raise subprocess.TimeoutExpired(cmd=argv, timeout=kwargs.get("timeout", 0))
            return _FakeCompleted(attach_returncode, stdout=b"hi\n", stderr=b"")
        if cmd in ("rm", "kill", "stats"):
            return _FakeCompleted(0, stdout=b"", stderr=b"")
        return _FakeCompleted(0)

    return fake_run


class _FakeStatsProc:
    """Stand-in for ``subprocess.Popen`` so the streaming-stats thread
    in ``run_constrained`` doesn't try to spawn a real ``docker stats``
    process during unit tests."""

    def __init__(self, lines: list[str] | None = None) -> None:
        self.stdout = iter(lines or [])
        self.terminated = False
        self.killed = False

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True

    def wait(self, timeout: float | None = None) -> int:
        return 0


def _patch_stats(lines: list[str] | None = None) -> Any:
    return patch("bench_kit.exec.subprocess.Popen", return_value=_FakeStatsProc(lines))


def test_run_constrained_happy_path() -> None:
    with (
        patch("bench_kit.exec.shutil.which", return_value="/usr/bin/docker"),
        patch("bench_kit.exec.subprocess.run", side_effect=_fake_run_factory()),
        _patch_stats(),
    ):
        res = run_constrained(
            image=_DIGEST,
            args=["echo", "hi"],
            network="n",
            mounts=[],
            limits=_LIMITS,
        )
    assert res.success is True
    assert res.exit_code == 0
    assert res.timed_out is False
    assert res.stdout == b"hi\n"


def test_run_constrained_nonzero_exit_is_not_success() -> None:
    with (
        patch("bench_kit.exec.shutil.which", return_value="/usr/bin/docker"),
        patch(
            "bench_kit.exec.subprocess.run",
            side_effect=_fake_run_factory(attach_returncode=2),
        ),
        _patch_stats(),
    ):
        res = run_constrained(
            image=_DIGEST,
            args=["false"],
            network="n",
            mounts=[],
            limits=_LIMITS,
        )
    assert res.success is False
    assert res.exit_code == 2
    assert res.timed_out is False


def test_run_constrained_timeout_kills_and_reports() -> None:
    with (
        patch("bench_kit.exec.shutil.which", return_value="/usr/bin/docker"),
        patch(
            "bench_kit.exec.subprocess.run",
            side_effect=_fake_run_factory(timeout=True),
        ),
        _patch_stats(),
    ):
        res = run_constrained(
            image=_DIGEST,
            args=["sleep", "1000"],
            network="n",
            mounts=[],
            limits=ExecLimits(memory_mb=64, cpus=1.0, timeout_s=1),
        )
    assert res.success is False
    assert res.timed_out is True
    assert res.exit_code == 124


def test_run_constrained_create_failure_raises() -> None:
    def fake_run(argv: list[str], **_: Any) -> Any:
        if argv[1] == "create":
            return _FakeCompleted(1, stdout=b"", stderr=b"image not found")
        return _FakeCompleted(0)

    with (
        patch("bench_kit.exec.shutil.which", return_value="/usr/bin/docker"),
        patch("bench_kit.exec.subprocess.run", side_effect=fake_run),
        _patch_stats(),
        pytest.raises(ExecError, match="docker create failed"),
    ):
        run_constrained(
            image=_DIGEST,
            args=[],
            network="n",
            mounts=[],
            limits=_LIMITS,
        )


# ---------------------------------------------------------------------------
# real Docker — gated
# ---------------------------------------------------------------------------

# Digest-pinned hello-world. This exact digest exists on Docker Hub and is
# stable; if Docker Hub purges it, refresh once and pin again.
_HELLO_WORLD = (
    "library/hello-world@sha256:ec153840d1e635ac434fab5e377081f17e0e15afab27beb3f726c3265039cfff"
)


def _docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        proc = subprocess.run(
            ["docker", "info", "--format", "{{json .ServerVersion}}"],
            capture_output=True,
            timeout=3,
            check=False,
        )
    except (subprocess.SubprocessError, OSError):
        return False
    return proc.returncode == 0


@pytest.mark.docker
@pytest.mark.skipif(not _docker_available(), reason="docker daemon not reachable")
def test_real_docker_hello_world() -> None:
    """End-to-end smoke against a real Docker daemon."""
    res = run_constrained(
        image=_HELLO_WORLD,
        args=[],
        network="bridge",  # hello-world doesn't talk to anything; bridge is fine for smoke
        mounts=[],
        limits=ExecLimits(memory_mb=64, cpus=0.5, timeout_s=30),
    )
    assert res.success is True, (res.exit_code, res.stderr)
    assert b"Hello from Docker" in res.stdout
    assert res.wall_clock_ms > 0
    # peak_rss may be 0 if the container exits before the first stats poll —
    # acceptable for a multi-millisecond hello-world. cpu_time_ms similarly.
    assert res.timed_out is False
