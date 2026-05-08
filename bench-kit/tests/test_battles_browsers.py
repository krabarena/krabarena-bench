"""Integration test: the canonical browsers Battle must always
``bench validate`` clean.

This test runs against the actual ``battles/browsers/`` directory in
the repo (resolved relative to the bench-kit checkout). It is the
guard that catches schema / lint / policy drift between bench-kit and
the first real Battle — if either side regresses, the other side's
PR will turn red.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bench_kit.validate import validate_battle

_REPO_ROOT = Path(__file__).resolve().parents[2]
_BATTLE_DIR = _REPO_ROOT / "battles" / "browsers"


@pytest.mark.skipif(not _BATTLE_DIR.is_dir(), reason="battles/browsers/ not in checkout")
def test_browsers_battle_validates_clean() -> None:
    report = validate_battle(_BATTLE_DIR)
    assert report.ok, "issues:\n" + "\n".join(str(i) for i in report.issues)
