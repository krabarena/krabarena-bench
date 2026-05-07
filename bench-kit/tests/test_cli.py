"""CLI smoke tests via direct invocation of ``main()``."""

from __future__ import annotations

from pathlib import Path

import pytest

from bench_kit.cli import (
    EXIT_NOT_IMPLEMENTED,
    EXIT_OK,
    EXIT_USAGE,
    EXIT_VALIDATION_FAILED,
    main,
)

_GOOD_UUID = "550e8400-e29b-41d4-a716-446655440000"


def test_version_prints_and_exits(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc_info:
        main(["--version"])
    assert exc_info.value.code == 0
    captured = capsys.readouterr()
    assert "bench-kit" in captured.out
    assert "spec" in captured.out


def test_no_command_prints_help(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main([])
    assert rc == EXIT_USAGE
    captured = capsys.readouterr()
    assert "bench" in captured.out


def test_init_battle_happy(tmp_path: Path) -> None:
    rc = main(
        [
            "init",
            "battle",
            "browsers",
            "--battle-id",
            _GOOD_UUID,
            "--out",
            str(tmp_path),
        ]
    )
    assert rc == EXIT_OK
    assert (tmp_path / "battles" / "browsers" / "meta.yaml").is_file()


def test_init_battle_bad_uuid(tmp_path: Path) -> None:
    rc = main(
        [
            "init",
            "battle",
            "browsers",
            "--battle-id",
            "not-a-uuid",
            "--out",
            str(tmp_path),
        ]
    )
    assert rc == EXIT_USAGE


def test_validate_clean_battle(tmp_path: Path) -> None:
    main(
        [
            "init",
            "battle",
            "browsers",
            "--battle-id",
            _GOOD_UUID,
            "--out",
            str(tmp_path),
        ]
    )
    rc = main(["validate", str(tmp_path / "battles" / "browsers")])
    assert rc == EXIT_OK


def test_validate_dirty_battle(tmp_path: Path) -> None:
    main(
        [
            "init",
            "battle",
            "browsers",
            "--battle-id",
            _GOOD_UUID,
            "--out",
            str(tmp_path),
        ]
    )
    target = tmp_path / "battles" / "browsers"
    (target / "meta.yaml").unlink()
    rc = main(["validate", str(target)])
    assert rc == EXIT_VALIDATION_FAILED


@pytest.mark.parametrize("cmd", ["verify"])
def test_unimplemented_subcommands_exit_with_code(cmd: str) -> None:
    rc = main([cmd])
    assert rc == EXIT_NOT_IMPLEMENTED
