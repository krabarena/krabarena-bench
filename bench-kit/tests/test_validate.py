"""End-to-end static validation tests.

We use ``init_battle`` to scaffold a clean Battle and assert it
validates green. Then mutate fields in isolation and assert each
mutation produces the expected issue code — this both proves the
scaffold is canonical and exercises every rule.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from bench_kit.init import init_battle
from bench_kit.validate import validate_battle

_GOOD_UUID = "550e8400-e29b-41d4-a716-446655440000"


@pytest.fixture
def clean_battle(tmp_path: Path) -> Path:
    return init_battle("browsers", _GOOD_UUID, parent_dir=tmp_path)


def _patch_yaml(path: Path, mutate: object) -> None:
    """Re-write a YAML file after applying ``mutate(data)``."""
    with path.open(encoding="utf-8") as f:
        data = yaml.safe_load(f)
    mutate(data)  # type: ignore[operator]
    with path.open("w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, sort_keys=False)


def test_scaffolded_battle_is_clean(clean_battle: Path) -> None:
    report = validate_battle(clean_battle)
    assert report.ok, [str(i) for i in report.issues]


def test_missing_battle_dir(tmp_path: Path) -> None:
    report = validate_battle(tmp_path / "nope")
    assert any(i.code == "BK040" for i in report.issues)


def test_missing_meta_yaml(clean_battle: Path) -> None:
    (clean_battle / "meta.yaml").unlink()
    report = validate_battle(clean_battle)
    assert any(i.code == "BK010" for i in report.issues)


def test_meta_schema_violation(clean_battle: Path) -> None:
    def mut(data: dict[str, object]) -> None:
        data["kind"] = "proxy-execution"  # only "containerised" allowed

    _patch_yaml(clean_battle / "meta.yaml", mut)
    report = validate_battle(clean_battle)
    assert any(i.code == "BK010" for i in report.issues)


def test_slug_dir_mismatch(clean_battle: Path) -> None:
    def mut(data: dict[str, object]) -> None:
        data["slug"] = "different-slug"

    _patch_yaml(clean_battle / "meta.yaml", mut)
    report = validate_battle(clean_battle)
    assert any(i.code == "BK011" for i in report.issues)


def test_task_id_mismatch(clean_battle: Path) -> None:
    def mut(data: dict[str, object]) -> None:
        data["id"] = "not-the-filename"

    _patch_yaml(clean_battle / "tasks" / "example.yaml", mut)
    report = validate_battle(clean_battle)
    assert any(i.code == "BK021" for i in report.issues)


def test_task_fixture_service_not_in_compose(clean_battle: Path) -> None:
    def mut(data: dict[str, object]) -> None:
        data["fixture_service"] = "not-declared"

    _patch_yaml(clean_battle / "tasks" / "example.yaml", mut)
    report = validate_battle(clean_battle)
    assert any(i.code == "BK022" for i in report.issues)


def test_compose_network_not_internal(clean_battle: Path) -> None:
    def mut(data: dict[str, object]) -> None:
        nets = data["networks"]
        assert isinstance(nets, dict)
        nets["bench"] = {}  # missing internal: true

    _patch_yaml(clean_battle / "fixtures" / "compose.yml", mut)
    report = validate_battle(clean_battle)
    assert any(i.code == "BK031" for i in report.issues)


def test_compose_image_not_digest_pinned(clean_battle: Path) -> None:
    def mut(data: dict[str, object]) -> None:
        services = data["services"]
        assert isinstance(services, dict)
        services["example"]["image"] = "library/hello-world:latest"

    _patch_yaml(clean_battle / "fixtures" / "compose.yml", mut)
    report = validate_battle(clean_battle)
    assert any(i.code == "BK033" for i in report.issues)


def test_runner_with_forbidden_import_rejected(clean_battle: Path) -> None:
    runner_path = clean_battle / "runners" / "example.py"
    body = runner_path.read_text(encoding="utf-8")
    runner_path.write_text("import subprocess\n" + body, encoding="utf-8")
    report = validate_battle(clean_battle)
    assert any(i.code == "BK001" for i in report.issues)
