"""Reproduce a Claim bundle and diff against the recorded result.

``bench verify`` is the second half of the Claim/Verify loop. Given a
``claim.tar.gz`` and a local checkout of the Battle repo at the same
commit, it:

1. extracts the bundle to a temp dir;
2. locates the Battle directory in the source checkout (matching
   ``battle_id`` from ``meta.yaml``);
3. re-runs the Battle with the same orchestrator (``run_battle``);
4. compares the verifier's ``summary`` against the bundle's
   ``summary`` per (tool, task, metric), under the tolerances declared
   in the Battle's ``meta.yaml``;
5. emits a ``verify-result.json`` with verdict + per-metric diff
   that the agent feeds to ``krab verify --from-bench-result``.

Out of scope for this commit
- Auto-cloning ``battle_repo`` at ``battle_commit``. The verifier
  must point ``--source`` at a checkout already at the right SHA.
  Rationale: makes the unit tests deterministic and lets us land
  the diff logic without network in CI. A follow-up PR adds
  ``--auto-clone``.

Tolerance contract (SPEC §7)
- ``success_rate`` must match exactly. Different success rates mean
  the tool behaved differently — the primary signal of refutation.
- All other metrics are within tolerance iff
  ``abs(verified - claimed) / max(claimed, 1) <= tolerance``.
- Tolerances come from the Battle's ``meta.yaml`` ``tolerances:``
  block; the schema's ``maximum:`` already caps them at the
  bench-kit defaults.
"""

from __future__ import annotations

import gzip
import json
import tarfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from bench_kit import SPEC_VERSION
from bench_kit.package import BundleMeta, PackageError, read_bundle_meta
from bench_kit.run import RunOptions, run_battle

VERDICT_MATCH = "match"
VERDICT_MISMATCH = "mismatch"
VERDICT_INCOMPLETE = "incomplete"


class VerifyError(Exception):
    """Raised when verification cannot proceed (setup failure)."""


@dataclass(frozen=True, slots=True)
class MetricDiff:
    """One (tool, task, metric) comparison."""

    tool: str
    task: str
    metric: str
    claimed: float | int
    verified: float | int | None
    rel_diff: float | None
    tolerance: float
    within_tolerance: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "tool": self.tool,
            "task": self.task,
            "metric": self.metric,
            "claimed": self.claimed,
            "verified": self.verified,
            "rel_diff": self.rel_diff,
            "tolerance": self.tolerance,
            "within_tolerance": self.within_tolerance,
        }


@dataclass(frozen=True, slots=True)
class VerifyReport:
    """Aggregate verification result.

    ``verdict`` is one of ``match``, ``mismatch``, ``incomplete``.
    """

    verdict: str
    bundle_meta: BundleMeta
    diffs: list[MetricDiff] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SPEC_VERSION,
            "verdict": self.verdict,
            "bundle_meta": self.bundle_meta.to_dict(),
            "diffs": [d.to_dict() for d in self.diffs],
        }


def verify_bundle(
    bundle_path: Path,
    *,
    source_dir: Path,
    output_path: Path | None = None,
) -> VerifyReport:
    """Reproduce ``bundle_path`` against ``source_dir`` and emit a report.

    ``source_dir`` is a local checkout of ``battle_repo`` at exactly
    ``battle_commit`` (caller's responsibility). The returned
    :class:`VerifyReport` is also written as JSON to ``output_path``
    if provided.
    """
    if not bundle_path.is_file():
        msg = f"bundle does not exist: {bundle_path}"
        raise VerifyError(msg)
    if not source_dir.is_dir():
        msg = f"source directory does not exist: {source_dir}"
        raise VerifyError(msg)

    try:
        meta = read_bundle_meta(bundle_path)
    except PackageError as exc:
        msg = f"could not read bundle meta: {exc}"
        raise VerifyError(msg) from exc

    claimed_result = _extract_result_json(bundle_path)
    battle_dir = _locate_battle(source_dir, meta.battle_id)

    if not _commit_matches(source_dir, meta.battle_commit):
        msg = (
            f"--source {source_dir} is not at commit {meta.battle_commit[:12]}; "
            f"check out the right SHA before verifying"
        )
        raise VerifyError(msg)

    tolerances = _load_tolerances(battle_dir)

    verified = run_battle(battle_dir, RunOptions(battle_repo=meta.battle_repo))
    verified_result = json.loads(verified.read_text(encoding="utf-8"))

    diffs = _compare_summaries(
        claimed=claimed_result["summary"],
        verified=verified_result["summary"],
        tolerances=tolerances,
    )
    verdict = _verdict_from_diffs(diffs)
    report = VerifyReport(verdict=verdict, bundle_meta=meta, diffs=diffs)

    if output_path is not None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(
            json.dumps(report.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    return report


def _extract_result_json(bundle_path: Path) -> dict[str, Any]:
    with gzip.open(bundle_path, "rb") as gz, tarfile.open(fileobj=gz, mode="r") as tar:
        try:
            entry = tar.getmember("result.json")
        except KeyError as exc:
            msg = "bundle is missing result.json"
            raise VerifyError(msg) from exc
        f = tar.extractfile(entry)
        if f is None:
            msg = "bundle result.json could not be opened"
            raise VerifyError(msg)
        out: dict[str, Any] = json.loads(f.read().decode("utf-8"))
        return out


def _locate_battle(source_dir: Path, battle_id: str) -> Path:
    """Find the ``battles/<slug>/`` whose ``meta.yaml`` declares this id."""
    battles_root = source_dir / "battles"
    if not battles_root.is_dir():
        msg = f"source has no battles/ directory: {source_dir}"
        raise VerifyError(msg)
    for child in sorted(battles_root.iterdir()):
        if not child.is_dir():
            continue
        meta_path = child / "meta.yaml"
        if not meta_path.is_file():
            continue
        try:
            data = yaml.safe_load(meta_path.read_text(encoding="utf-8"))
        except yaml.YAMLError:
            continue
        if isinstance(data, dict) and data.get("battle_id") == battle_id:
            return child
    msg = f"no battle in {source_dir}/battles/ has battle_id={battle_id}"
    raise VerifyError(msg)


def _commit_matches(source_dir: Path, expected: str) -> bool:
    import subprocess  # noqa: PLC0415 — keep at use-site so import-cost stays out of common path

    proc = subprocess.run(
        ["git", "-C", str(source_dir), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    return proc.returncode == 0 and proc.stdout.strip() == expected


def _load_tolerances(battle_dir: Path) -> dict[str, float]:
    meta = yaml.safe_load((battle_dir / "meta.yaml").read_text(encoding="utf-8"))
    tolerances = meta.get("tolerances") or {}
    return {k: float(v) for k, v in tolerances.items()}


def _compare_summaries(
    *,
    claimed: list[dict[str, Any]],
    verified: list[dict[str, Any]],
    tolerances: dict[str, float],
) -> list[MetricDiff]:
    verified_by_key: dict[tuple[str, str], dict[str, Any]] = {
        (row["tool"], row["task"]): row["metrics"] for row in verified
    }
    diffs: list[MetricDiff] = []
    for row in claimed:
        tool, task = row["tool"], row["task"]
        v_metrics = verified_by_key.get((tool, task))
        for metric, claimed_value in row["metrics"].items():
            tol = tolerances.get(metric, 0.0)
            if v_metrics is None:
                diffs.append(
                    MetricDiff(
                        tool=tool,
                        task=task,
                        metric=metric,
                        claimed=claimed_value,
                        verified=None,
                        rel_diff=None,
                        tolerance=tol,
                        within_tolerance=False,
                    )
                )
                continue
            verified_value = v_metrics.get(metric)
            diffs.append(_one_diff(tool, task, metric, claimed_value, verified_value, tol))
    return diffs


def _one_diff(
    tool: str,
    task: str,
    metric: str,
    claimed_value: float | int,
    verified_value: float | int | None,
    tolerance: float,
) -> MetricDiff:
    if verified_value is None:
        return MetricDiff(
            tool=tool,
            task=task,
            metric=metric,
            claimed=claimed_value,
            verified=None,
            rel_diff=None,
            tolerance=tolerance,
            within_tolerance=False,
        )
    # success_rate is special: tolerance must be exactly 0, comparing equality.
    if metric == "success_rate":
        within = float(claimed_value) == float(verified_value)
        rel = 0.0 if within else 1.0
        return MetricDiff(
            tool=tool,
            task=task,
            metric=metric,
            claimed=claimed_value,
            verified=verified_value,
            rel_diff=rel,
            tolerance=0.0,
            within_tolerance=within,
        )
    denom = abs(float(claimed_value)) if claimed_value else 1.0
    rel = abs(float(verified_value) - float(claimed_value)) / denom
    return MetricDiff(
        tool=tool,
        task=task,
        metric=metric,
        claimed=claimed_value,
        verified=verified_value,
        rel_diff=round(rel, 6),
        tolerance=tolerance,
        within_tolerance=rel <= tolerance,
    )


def _verdict_from_diffs(diffs: list[MetricDiff]) -> str:
    if not diffs:
        return VERDICT_INCOMPLETE
    if any(d.verified is None for d in diffs):
        return VERDICT_INCOMPLETE
    if all(d.within_tolerance for d in diffs):
        return VERDICT_MATCH
    return VERDICT_MISMATCH


__all__ = [
    "MetricDiff",
    "VerifyError",
    "VerifyReport",
    "verify_bundle",
]
