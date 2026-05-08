"""Tests for the bundle verifier.

The verifier's interesting logic is the diff/verdict computation; the
end-to-end git-clone-and-rerun flow is exercised in the per-Battle
integration that lands in PR 5+. Here we focus on:

* :func:`_compare_summaries` covers all branches
  (within tolerance, outside, success_rate exact match, missing row).
* :func:`_verdict_from_diffs` returns the right verdict for each
  shape of input.
"""

from __future__ import annotations

import gzip
import io
import json
import tarfile
from pathlib import Path

import pytest

from bench_kit.verify import (
    VERDICT_INCOMPLETE,
    VERDICT_MATCH,
    VERDICT_MISMATCH,
    MetricDiff,
    VerifyError,
    _auto_clone,
    _compare_summaries,
    _resolve_clone_url,
    _verdict_from_diffs,
    verify_bundle,
)

_TOL = {
    "success_rate": 0.0,
    "wall_clock_ms_p50": 0.20,
    "peak_rss_mb": 0.25,
}


def _row(tool: str, task: str, **metrics: float | int) -> dict[str, object]:
    return {"tool": tool, "task": task, "metrics": dict(metrics)}


# ---------------------------------------------------------------------------
# diff logic
# ---------------------------------------------------------------------------


def test_diff_within_tolerance_is_match() -> None:
    claimed = [_row("a", "scroll", success_rate=1.0, wall_clock_ms_p50=100, peak_rss_mb=10)]
    verified = [_row("a", "scroll", success_rate=1.0, wall_clock_ms_p50=110, peak_rss_mb=11)]
    diffs = _compare_summaries(claimed=claimed, verified=verified, tolerances=_TOL)
    assert all(d.within_tolerance for d in diffs)
    assert _verdict_from_diffs(diffs) == VERDICT_MATCH


def test_diff_outside_tolerance_is_mismatch() -> None:
    claimed = [_row("a", "scroll", success_rate=1.0, wall_clock_ms_p50=100, peak_rss_mb=10)]
    verified = [_row("a", "scroll", success_rate=1.0, wall_clock_ms_p50=200, peak_rss_mb=10)]
    diffs = _compare_summaries(claimed=claimed, verified=verified, tolerances=_TOL)
    bad = next(d for d in diffs if d.metric == "wall_clock_ms_p50")
    assert bad.within_tolerance is False
    assert _verdict_from_diffs(diffs) == VERDICT_MISMATCH


def test_success_rate_must_match_exactly() -> None:
    claimed = [_row("a", "scroll", success_rate=1.0)]
    verified = [_row("a", "scroll", success_rate=0.95)]
    diffs = _compare_summaries(claimed=claimed, verified=verified, tolerances=_TOL)
    sr = next(d for d in diffs if d.metric == "success_rate")
    assert sr.within_tolerance is False
    assert _verdict_from_diffs(diffs) == VERDICT_MISMATCH


def test_missing_verified_row_is_incomplete() -> None:
    claimed = [_row("a", "scroll", success_rate=1.0, wall_clock_ms_p50=100)]
    verified: list[dict[str, object]] = []
    diffs = _compare_summaries(claimed=claimed, verified=verified, tolerances=_TOL)
    assert all(d.verified is None for d in diffs)
    assert _verdict_from_diffs(diffs) == VERDICT_INCOMPLETE


def test_missing_metric_in_verified_is_incomplete() -> None:
    claimed = [_row("a", "scroll", success_rate=1.0, peak_rss_mb=10)]
    verified = [_row("a", "scroll", success_rate=1.0)]  # peak_rss_mb absent
    diffs = _compare_summaries(claimed=claimed, verified=verified, tolerances=_TOL)
    assert _verdict_from_diffs(diffs) == VERDICT_INCOMPLETE


def test_unknown_metric_uses_zero_tolerance() -> None:
    """A claimed metric the verifier reports identically still matches at tol 0.0."""
    claimed = [_row("a", "scroll", custom_score=42)]
    verified = [_row("a", "scroll", custom_score=42)]
    diffs = _compare_summaries(claimed=claimed, verified=verified, tolerances=_TOL)
    assert all(d.within_tolerance for d in diffs)
    # And a 1-unit drift would fail since unknown metric has implicit 0 tolerance.
    verified2 = [_row("a", "scroll", custom_score=43)]
    diffs2 = _compare_summaries(claimed=claimed, verified=verified2, tolerances=_TOL)
    assert _verdict_from_diffs(diffs2) == VERDICT_MISMATCH


def test_metric_diff_to_dict_is_serialisable() -> None:
    d = MetricDiff(
        tool="a",
        task="b",
        metric="m",
        claimed=10,
        verified=11,
        rel_diff=0.1,
        tolerance=0.2,
        within_tolerance=True,
    )
    out = d.to_dict()
    assert out["tool"] == "a"
    assert out["within_tolerance"] is True


# ---------------------------------------------------------------------------
# entrypoint guards
# ---------------------------------------------------------------------------


def test_verify_bundle_missing_bundle(tmp_path: Path) -> None:
    with pytest.raises(VerifyError, match="bundle does not exist"):
        verify_bundle(tmp_path / "nope.tar.gz", source_dir=tmp_path)


def _write_minimal_bundle(path: Path) -> None:
    """Write a tar.gz with just meta.json — enough for meta-read to succeed."""
    meta = {
        "bundle_version": "0.1.0",
        "battle_id": "550e8400-e29b-41d4-a716-446655440000",
        "battle_repo": "github.com/example/repo",
        "battle_commit": "0" * 40,
        "bench_kit_version": "0.1.0",
        "ran_at": "2026-05-08T00:00:00Z",
        "schema_version": "0.1.0",
    }
    raw = json.dumps(meta).encode("utf-8")
    with (
        path.open("wb") as fh,
        gzip.GzipFile(filename="", fileobj=fh, mtime=0, mode="wb") as gz,
        tarfile.open(fileobj=gz, mode="w") as tar,
    ):
        info = tarfile.TarInfo("meta.json")
        info.size = len(raw)
        info.mtime = 0
        tar.addfile(info, io.BytesIO(raw))


def test_verify_bundle_missing_source(tmp_path: Path) -> None:
    bundle = tmp_path / "claim.tar.gz"
    _write_minimal_bundle(bundle)
    with pytest.raises(VerifyError, match="source directory does not exist"):
        verify_bundle(bundle, source_dir=tmp_path / "nope")


def test_verify_source_and_keep_source_mutually_exclusive(tmp_path: Path) -> None:
    bundle = tmp_path / "claim.tar.gz"
    bundle.write_bytes(b"fake")
    src = tmp_path / "src"
    keep = tmp_path / "keep"
    with pytest.raises(VerifyError, match="mutually exclusive"):
        verify_bundle(bundle, source_dir=src, keep_source=keep)


def test_verify_keep_source_must_be_empty(tmp_path: Path) -> None:
    """A non-empty --keep-source destination is refused before any clone."""
    bundle = tmp_path / "claim.tar.gz"
    _write_minimal_bundle(bundle)
    keep = tmp_path / "keep"
    keep.mkdir()
    (keep / "stale.txt").write_text("oops", encoding="utf-8")
    with pytest.raises(VerifyError, match="must be empty"):
        verify_bundle(bundle, keep_source=keep)


# ---------------------------------------------------------------------------
# clone-url resolution
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("repo", "expected"),
    [
        (
            "github.com/keenableai/krabarena-bench",
            "https://github.com/keenableai/krabarena-bench.git",
        ),
        (
            "https://github.com/keenableai/krabarena-bench",
            "https://github.com/keenableai/krabarena-bench.git",
        ),
        (
            "https://github.com/keenableai/krabarena-bench.git",
            "https://github.com/keenableai/krabarena-bench.git",
        ),
        ("http://example.com/foo", "http://example.com/foo.git"),
    ],
)
def test_resolve_clone_url_accepts(repo: str, expected: str) -> None:
    assert _resolve_clone_url(repo) == expected


# ---------------------------------------------------------------------------
# real-network integration (gated)
# ---------------------------------------------------------------------------


# The merge commit of PR #1 — first stable SHA on `main` of this very
# repo. Pinned here forever; if rebased into oblivion (it won't be —
# we'd preserve merge history), refresh.
_FIRST_STABLE_SHA = "22887f9dc4503abc058f7f734d5f367ac12191a0"


@pytest.mark.network
def test_auto_clone_real_repo(tmp_path: Path) -> None:
    """Pulls a known-good SHA out of the project's own GitHub repo."""
    target = tmp_path / "src"
    cloned = _auto_clone(
        "github.com/keenableai/krabarena-bench",
        _FIRST_STABLE_SHA,
        target,
    )
    assert (cloned / "SPEC.md").is_file()
    assert (cloned / "bench-kit" / "bench_kit" / "__init__.py").is_file()


@pytest.mark.network
def test_auto_clone_rejects_bogus_commit(tmp_path: Path) -> None:
    """Server-side rejection of an unknown SHA must surface as VerifyError."""
    target = tmp_path / "src"
    with pytest.raises(VerifyError):
        _auto_clone(
            "github.com/keenableai/krabarena-bench",
            "deadbeef" * 5,  # 40-hex but not a real commit
            target,
        )


@pytest.mark.parametrize(
    "bad",
    [
        "git@github.com:keenableai/krabarena-bench.git",
        "ssh://git@github.com/keenableai/krabarena-bench.git",
        "file:///etc/passwd",
        "",
        "   ",
        "../etc/passwd",
        "weird; rm -rf /",
    ],
)
def test_resolve_clone_url_rejects(bad: str) -> None:
    with pytest.raises(VerifyError):
        _resolve_clone_url(bad)
