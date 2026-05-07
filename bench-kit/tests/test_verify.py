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

from pathlib import Path

import pytest

from bench_kit.verify import (
    VERDICT_INCOMPLETE,
    VERDICT_MATCH,
    VERDICT_MISMATCH,
    MetricDiff,
    VerifyError,
    _compare_summaries,
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


def test_verify_bundle_missing_source(tmp_path: Path) -> None:
    bundle = tmp_path / "claim.tar.gz"
    bundle.write_bytes(b"fake")
    with pytest.raises(VerifyError, match="source directory does not exist"):
        verify_bundle(bundle, source_dir=tmp_path / "nope")
