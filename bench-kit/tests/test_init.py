"""Tests for the battle directory scaffolding."""

from __future__ import annotations

from pathlib import Path

import pytest

from bench_kit.init import InitError, init_battle, plan

_GOOD_UUID = "550e8400-e29b-41d4-a716-446655440000"


def test_plan_lists_expected_files(tmp_path: Path) -> None:
    p = plan("browsers", _GOOD_UUID, tmp_path)
    relpaths = {str(rel) for rel in p.files}
    assert relpaths == {
        "meta.yaml",
        "README.md",
        "tasks/example.yaml",
        "runners/example.py",
        "fixtures/compose.yml",
        "Dockerfile",
    }
    assert p.target_dir == (tmp_path / "battles" / "browsers")


def test_init_battle_creates_files(tmp_path: Path) -> None:
    target = init_battle("browsers", _GOOD_UUID, parent_dir=tmp_path)
    assert (target / "meta.yaml").is_file()
    assert (target / "tasks" / "example.yaml").is_file()
    assert (target / "runners" / "example.py").is_file()
    assert (target / "fixtures" / "compose.yml").is_file()
    meta_text = (target / "meta.yaml").read_text(encoding="utf-8")
    assert _GOOD_UUID in meta_text
    assert 'slug: "browsers"' in meta_text


@pytest.mark.parametrize(
    "bad_slug",
    [
        "Browsers",  # uppercase
        "ab",  # too short
        "1browsers",  # leading digit
        "browsers!",  # invalid char
        "a" * 81,  # too long
    ],
)
def test_init_rejects_bad_slug(tmp_path: Path, bad_slug: str) -> None:
    with pytest.raises(InitError):
        init_battle(bad_slug, _GOOD_UUID, parent_dir=tmp_path)


@pytest.mark.parametrize(
    "bad_uuid",
    [
        "not-a-uuid",
        "00000000-0000-0000-0000-000000000000",  # nil UUID
        "550e8400e29b41d4a716446655440000",  # missing hyphens
    ],
)
def test_init_rejects_bad_battle_id(tmp_path: Path, bad_uuid: str) -> None:
    with pytest.raises(InitError):
        init_battle("browsers", bad_uuid, parent_dir=tmp_path)


def test_init_refuses_to_overwrite_nonempty(tmp_path: Path) -> None:
    target_parent = tmp_path / "battles" / "browsers"
    target_parent.mkdir(parents=True)
    (target_parent / "existing.txt").write_text("not empty", encoding="utf-8")
    with pytest.raises(InitError, match="non-empty"):
        init_battle("browsers", _GOOD_UUID, parent_dir=tmp_path)
