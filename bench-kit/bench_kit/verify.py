"""Reproduce a Claim bundle and diff against the recorded result.

``bench verify`` is the second half of the Claim/Verify loop. Given a
``claim.tar.gz`` it:

1. opens the bundle, reads ``meta.json``;
2. obtains a checkout of ``battle_repo`` at ``battle_commit`` — by
   default a fresh ``git clone`` into a temporary directory; or, when
   ``--source`` / ``--keep-source`` is passed, an existing local
   checkout the caller manages;
3. locates the matching ``battles/<slug>/`` in that checkout;
4. re-runs the Battle through the same orchestrator that produced
   the original bundle (``run_battle``);
5. compares the verifier's ``summary`` against the bundle's
   ``summary`` per ``(tool, task, metric)``, under the tolerances
   declared in the Battle's ``meta.yaml``;
6. emits a ``verify-result.json`` with verdict + per-metric diff
   that the agent feeds to ``krab verify --from-bench-result``.

Trust model
-----------

The clone is always taken **independently from the canonical Git
host** — the bundle never carries source code. A malicious Claimer
who modified their local repo cannot influence what the verifier
runs; the recorded ``battle_commit`` is content-addressed by Git so
the verifier always reproduces against the exact bytes the Claim
declared. See ``docs/REPRODUCIBILITY.md`` for the long-form
explanation.

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

import json
import re
import shutil
import subprocess
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

import yaml

from bench_kit import SPEC_VERSION
from bench_kit.package import BundleMeta, PackageError, read_bundle_json, read_bundle_meta
from bench_kit.run import RunOptions, run_battle

Verdict = Literal["match", "mismatch", "incomplete"]
VERDICT_MATCH: Verdict = "match"
VERDICT_MISMATCH: Verdict = "mismatch"
VERDICT_INCOMPLETE: Verdict = "incomplete"

_REPO_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9._/-]*$", flags=re.IGNORECASE)
_GIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_TIMEOUT_CLONE_S = 300.0
_TIMEOUT_CHECKOUT_S = 60.0
_TIMEOUT_REVPARSE_S = 5.0


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
        return asdict(self)


@dataclass(frozen=True, slots=True)
class VerifyReport:
    """Aggregate verification result.

    ``verdict`` is one of ``match``, ``mismatch``, ``incomplete``.
    """

    verdict: Verdict
    bundle_meta: BundleMeta
    diffs: list[MetricDiff] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SPEC_VERSION,
            "verdict": self.verdict,
            "bundle_meta": self.bundle_meta.to_dict(),
            "diffs": [asdict(d) for d in self.diffs],
        }


def verify_bundle(
    bundle_path: Path,
    *,
    source_dir: Path | None = None,
    keep_source: Path | None = None,
    output_path: Path | None = None,
) -> VerifyReport:
    """Reproduce ``bundle_path`` and return a :class:`VerifyReport`.

    Source-of-truth dispatch:

    * ``source_dir`` set: use the existing local checkout. The caller
      is responsible for it being at the right commit; the function
      asserts that and raises if not.
    * ``keep_source`` set: clone the canonical repo at the recorded
      commit into ``keep_source`` (which must be empty) and leave it
      there. Useful for inspection or repeated verifies.
    * Both unset: clone into a fresh ``tempfile.TemporaryDirectory``
      and remove it on exit. The default path.

    ``source_dir`` and ``keep_source`` are mutually exclusive.
    """
    if source_dir is not None and keep_source is not None:
        msg = "--source and --keep-source are mutually exclusive"
        raise VerifyError(msg)

    if not bundle_path.is_file():
        msg = f"bundle does not exist: {bundle_path}"
        raise VerifyError(msg)

    try:
        meta = read_bundle_meta(bundle_path)
    except PackageError as exc:
        msg = f"could not read bundle meta: {exc}"
        raise VerifyError(msg) from exc

    if source_dir is not None:
        if not source_dir.is_dir():
            msg = f"source directory does not exist: {source_dir}"
            raise VerifyError(msg)
        return _verify_with_source(bundle_path, meta, source_dir, output_path)

    if keep_source is not None:
        if keep_source.exists() and not keep_source.is_dir():
            msg = f"--keep-source {keep_source} exists and is not a directory"
            raise VerifyError(msg)
        keep_source.mkdir(parents=True, exist_ok=True)
        if any(keep_source.iterdir()):
            msg = f"--keep-source {keep_source} must be empty"
            raise VerifyError(msg)
        cloned = _auto_clone(meta.battle_repo, meta.battle_commit, keep_source)
        return _verify_with_source(bundle_path, meta, cloned, output_path)

    with tempfile.TemporaryDirectory(prefix="krabbench-verify-") as tmp:
        target = Path(tmp) / "src"
        cloned = _auto_clone(meta.battle_repo, meta.battle_commit, target)
        return _verify_with_source(bundle_path, meta, cloned, output_path)


def _verify_with_source(
    bundle_path: Path,
    meta: BundleMeta,
    source_dir: Path,
    output_path: Path | None,
) -> VerifyReport:
    """Body of the verify pipeline given an already-resolved source tree."""
    claimed_result = _extract_result_json(bundle_path)
    battle_dir = _locate_battle(source_dir, meta.battle_id)

    if not _commit_matches(source_dir, meta.battle_commit):
        msg = (
            f"source {source_dir} is not at commit {meta.battle_commit[:12]}; "
            f"checkout drift detected"
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


# ---------------------------------------------------------------------------
# auto-clone
# ---------------------------------------------------------------------------


def _resolve_clone_url(repo: str) -> str:
    """Convert a ``battle_repo`` field to a git clone URL.

    Accepts the canonical slug form ``github.com/<owner>/<name>`` and
    full ``https://`` URLs (with or without ``.git`` suffix). Refuses
    everything else — plain HTTP (MITM-tamperable), SSH (``git@…``,
    needs verifier-side keys), ``file://``, and any other scheme.
    Verifiers who need a non-https source should clone manually and
    pass ``--source``.
    """
    repo = repo.strip()
    if not repo:
        msg = "battle_repo is empty"
        raise VerifyError(msg)
    if repo.startswith("https://"):
        return repo if repo.endswith(".git") else f"{repo}.git"
    if "://" in repo or repo.startswith("git@"):
        # Includes http:// — plain HTTP is rejected because verify
        # *executes* the cloned runner code, and MITM tampering on
        # an http transport silently substitutes whatever the
        # attacker wants run on the verifier's host.
        msg = (
            f"battle_repo {repo!r} must use https://; plain http, ssh, file "
            f"and other schemes are refused (clone manually and pass --source)"
        )
        raise VerifyError(msg)
    if not _REPO_SLUG_RE.match(repo):
        msg = f"battle_repo {repo!r} is not a recognised slug"
        raise VerifyError(msg)
    return f"https://{repo}.git"


def _auto_clone(repo: str, commit: str, target: Path) -> Path:
    """Fetch ``repo`` at exactly ``commit`` into ``target``.

    Tries a shallow fetch-by-SHA first (one commit, no history, no
    tags) — orders of magnitude faster for any non-trivial repo and
    supported by GitHub plus most modern Git hosts. Falls back to a
    full ``git clone`` + ``git checkout`` if the server refuses
    (older Gitea / custom hosts without
    ``uploadpack.allowReachableSHA1InWant``).

    Raises :class:`VerifyError` if git is missing, both fetch paths
    fail, or the resulting HEAD does not match ``commit`` (defence
    against silent server-side rewrites).
    """
    if shutil.which("git") is None:
        msg = "git not found on PATH; install it or pass --source <local-checkout>"
        raise VerifyError(msg)
    if not _GIT_SHA_RE.match(commit):
        msg = f"battle_commit {commit!r} is not a 40-hex SHA-1"
        raise VerifyError(msg)

    url = _resolve_clone_url(repo)
    target.parent.mkdir(parents=True, exist_ok=True)

    try:
        _shallow_fetch(url, commit, target)
    except VerifyError as shallow_exc:
        # Wipe whatever partial state the shallow attempt left behind
        # (init may have run before fetch failed) and try the full
        # clone path.
        shutil.rmtree(target, ignore_errors=True)
        try:
            _full_clone(url, commit, target)
        except VerifyError as full_exc:
            msg = f"both shallow and full clone failed; shallow: {shallow_exc}; full: {full_exc}"
            raise VerifyError(msg) from full_exc

    head = _run_git(
        ["-C", str(target), "rev-parse", "HEAD"],
        timeout=_TIMEOUT_REVPARSE_S,
    ).stdout.strip()
    if head != commit:
        msg = f"checkout landed on {head[:12]} but bundle requires {commit[:12]}; aborting"
        raise VerifyError(msg)
    return target


def _shallow_fetch(url: str, commit: str, target: Path) -> None:
    """One-commit fetch — fastest path when the host supports it."""
    target.mkdir(parents=True, exist_ok=True)
    _run_git(["-C", str(target), "init", "--quiet"], timeout=_TIMEOUT_REVPARSE_S)
    _run_git(
        ["-C", str(target), "remote", "add", "origin", url],
        timeout=_TIMEOUT_REVPARSE_S,
    )
    _run_git(
        [
            "-C",
            str(target),
            "fetch",
            "--quiet",
            "--depth=1",
            "--no-tags",
            "origin",
            commit,
        ],
        timeout=_TIMEOUT_CLONE_S,
    )
    _run_git(
        ["-C", str(target), "checkout", "--quiet", "FETCH_HEAD"],
        timeout=_TIMEOUT_CHECKOUT_S,
    )


def _full_clone(url: str, commit: str, target: Path) -> None:
    """Full-history clone fallback for hosts that reject single-SHA fetch."""
    _run_git(["clone", "--quiet", url, str(target)], timeout=_TIMEOUT_CLONE_S)
    _run_git(
        ["-C", str(target), "checkout", "--quiet", commit],
        timeout=_TIMEOUT_CHECKOUT_S,
    )


def _run_git(args: list[str], *, timeout: float) -> subprocess.CompletedProcess[str]:
    """Single shape for every ``git`` invocation; converts failures to VerifyError."""
    try:
        proc = subprocess.run(
            ["git", *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        msg = f"git {_subcommand_for_message(args)} timed out after {timeout}s"
        raise VerifyError(msg) from exc
    if proc.returncode != 0:
        # Surface git's stderr verbatim — it's already user-friendly enough
        # (e.g. "fatal: Authentication failed", "fatal: reference is not a tree").
        msg = (
            f"git {_subcommand_for_message(args)} failed: "
            f"{proc.stderr.strip() or proc.stdout.strip()}"
        )
        raise VerifyError(msg)
    return proc


def _subcommand_for_message(args: list[str]) -> str:
    """Pick a human-readable subcommand label for an error message.

    Many of our calls are ``["-C", "<dir>", "<cmd>", ...]``; reporting
    ``args[0]`` would surface ``"git -C failed"``, which tells the
    user nothing. Skip the global flag pair so the actual subcommand
    (clone, fetch, checkout, rev-parse) reaches the log.
    """
    if len(args) >= 3 and args[0] == "-C":
        return args[2]
    return args[0] if args else "<empty>"


# ---------------------------------------------------------------------------
# bundle / source helpers (unchanged from PR 2 except for module imports)
# ---------------------------------------------------------------------------


def _extract_result_json(bundle_path: Path) -> dict[str, Any]:
    try:
        return read_bundle_json(bundle_path, "result.json")
    except PackageError as exc:
        msg = f"bundle is missing or corrupt result.json: {exc}"
        raise VerifyError(msg) from exc


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
    try:
        proc = _run_git(
            ["-C", str(source_dir), "rev-parse", "HEAD"],
            timeout=_TIMEOUT_REVPARSE_S,
        )
    except VerifyError:
        return False
    return proc.stdout.strip() == expected


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
            verified_value = v_metrics.get(metric) if v_metrics is not None else None
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


def _verdict_from_diffs(diffs: list[MetricDiff]) -> Verdict:
    if not diffs or any(d.verified is None for d in diffs):
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
