"""Tests for the Claim-bundle packager."""

from __future__ import annotations

import gzip
import json
import tarfile
from pathlib import Path

import pytest

from bench_kit.package import PackageError, package_bundle, read_bundle_meta

_GOOD_COMMIT = "abc123def4567890abc123def4567890abc12345"


def _write_minimal_result(result_path: Path, *, commit: str = _GOOD_COMMIT) -> None:
    """Build a result.json that conforms to result.schema.json."""
    result = {
        "schema_version": "0.1.0",
        "battle_id": "550e8400-e29b-41d4-a716-446655440000",
        "battle_repo": "github.com/krabarena/krabarena-bench",
        "battle_commit": commit,
        "ran_at": "2026-05-07T05:44:56Z",
        "env": {
            "os": "Linux 6.6.30",
            "arch": "x86_64",
            "cpu_model": "AMD Ryzen",
            "cpu_count": 16,
            "mem_total_mb": 32768,
            "docker_version": "27.0.0",
            "kernel_version": "6.6.30-1",
            "bench_kit_version": "0.1.0.dev0",
            "runtime_hash": "sha256:" + "0" * 64,
            "hostname_redacted": True,
        },
        "tools": [
            {
                "name": "fake",
                "image_digest": "sha256:" + "1" * 64,
                "version": "lib/fake",
                "runner_path": "runners/fake.py",
            }
        ],
        "runs": [
            {
                "tool": "fake",
                "task": "scroll",
                "iteration": 0,
                "metrics": {"wall_clock_ms": 100, "peak_rss_mb": 10, "cpu_time_ms": 50},
                "success": True,
                "logs_path": "runs/fake/scroll/0",
            }
        ],
        "summary": [
            {
                "tool": "fake",
                "task": "scroll",
                "metrics": {"success_rate": 1.0, "wall_clock_ms_p50": 100, "peak_rss_mb": 10},
            }
        ],
    }
    result_path.write_text(json.dumps(result), encoding="utf-8")


def _write_runs_log(results_dir: Path) -> None:
    log = results_dir / "runs" / "fake" / "scroll" / "0" / "log.jsonl"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text('{"ok": true}\n', encoding="utf-8")


def test_package_happy_path(tmp_path: Path) -> None:
    result_path = tmp_path / "result.json"
    _write_minimal_result(result_path)
    _write_runs_log(tmp_path)
    bundle = tmp_path / "claim.tar.gz"

    out = package_bundle(result_path, bundle)
    assert out == bundle
    assert bundle.is_file()

    with gzip.open(bundle, "rb") as gz, tarfile.open(fileobj=gz, mode="r") as tar:
        names = sorted(tar.getnames())
    assert "meta.json" in names
    assert "result.json" in names
    assert any(n.startswith("runs/") for n in names)


def test_package_meta_has_pointer_fields(tmp_path: Path) -> None:
    result_path = tmp_path / "result.json"
    _write_minimal_result(result_path)
    bundle = tmp_path / "claim.tar.gz"
    package_bundle(result_path, bundle)

    meta = read_bundle_meta(bundle)
    assert meta.battle_repo == "github.com/krabarena/krabarena-bench"
    assert meta.battle_commit == _GOOD_COMMIT
    assert meta.bundle_version == "0.1.0"
    assert meta.bench_kit_version  # non-empty


def test_package_rejects_nil_commit(tmp_path: Path) -> None:
    result_path = tmp_path / "result.json"
    _write_minimal_result(result_path, commit="0" * 40)
    bundle = tmp_path / "claim.tar.gz"
    with pytest.raises(PackageError, match="nil battle_commit"):
        package_bundle(result_path, bundle)


def test_package_rejects_missing_result(tmp_path: Path) -> None:
    with pytest.raises(PackageError, match="does not exist"):
        package_bundle(tmp_path / "missing.json", tmp_path / "claim.tar.gz")


def test_package_rejects_schema_invalid_result(tmp_path: Path) -> None:
    result = tmp_path / "result.json"
    _write_minimal_result(result)
    data = json.loads(result.read_text(encoding="utf-8"))
    del data["env"]  # required by result.schema.json
    result.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(PackageError, match="does not conform to the schema"):
        package_bundle(result, tmp_path / "claim.tar.gz")


def test_package_rejects_non_json(tmp_path: Path) -> None:
    bad = tmp_path / "result.json"
    bad.write_text("not json", encoding="utf-8")
    with pytest.raises(PackageError, match="not valid JSON"):
        package_bundle(bad, tmp_path / "claim.tar.gz")


def test_package_is_byte_deterministic(tmp_path: Path) -> None:
    """Same inputs → same bundle bytes. Lets verifiers hash bundles."""
    result_path = tmp_path / "result.json"
    _write_minimal_result(result_path)
    _write_runs_log(tmp_path)

    a = tmp_path / "a.tar.gz"
    b = tmp_path / "b.tar.gz"
    package_bundle(result_path, a)
    package_bundle(result_path, b)
    assert a.read_bytes() == b.read_bytes()


# ---------------------------------------------------------------------------
# optional doc files (README.md, RUN.md, structure.json)
# ---------------------------------------------------------------------------


def test_package_with_all_extras(tmp_path: Path) -> None:
    result_path = tmp_path / "result.json"
    _write_minimal_result(result_path)
    readme = tmp_path / "README.md"
    readme.write_text("# Title\n\nBody.\n", encoding="utf-8")
    runbook = tmp_path / "RUN.md"
    runbook.write_text("Run with: bench run .\n", encoding="utf-8")
    structure = tmp_path / "structure.json"
    structure.write_text('{"schema_version": 1}', encoding="utf-8")

    bundle = tmp_path / "claim.tar.gz"
    package_bundle(
        result_path,
        bundle,
        readme=readme,
        runbook=runbook,
        structure=structure,
    )

    with gzip.open(bundle, "rb") as gz, tarfile.open(fileobj=gz, mode="r") as tar:
        names = sorted(tar.getnames())
    assert "README.md" in names
    assert "RUN.md" in names
    assert "structure.json" in names
    assert "meta.json" in names
    assert "result.json" in names


def test_package_without_extras_keeps_pointer_only_shape(tmp_path: Path) -> None:
    """Back-compat: package_bundle without extras produces the original shape."""
    result_path = tmp_path / "result.json"
    _write_minimal_result(result_path)
    bundle = tmp_path / "claim.tar.gz"

    package_bundle(result_path, bundle)

    with gzip.open(bundle, "rb") as gz, tarfile.open(fileobj=gz, mode="r") as tar:
        names = sorted(tar.getnames())
    assert "README.md" not in names
    assert "RUN.md" not in names
    assert "structure.json" not in names


def test_package_rejects_missing_extra(tmp_path: Path) -> None:
    """A passed --readme/--runbook/--structure that points nowhere fails loudly."""
    result_path = tmp_path / "result.json"
    _write_minimal_result(result_path)
    bundle = tmp_path / "claim.tar.gz"

    with pytest.raises(PackageError, match=r"readme.*does not exist"):
        package_bundle(result_path, bundle, readme=tmp_path / "nope.md")


def test_package_rejects_invalid_structure_json(tmp_path: Path) -> None:
    """structure.json must parse as JSON before we put it into the bundle."""
    result_path = tmp_path / "result.json"
    _write_minimal_result(result_path)
    bad = tmp_path / "structure.json"
    bad.write_text("not json {", encoding="utf-8")
    bundle = tmp_path / "claim.tar.gz"

    with pytest.raises(PackageError, match=r"structure.*not valid JSON"):
        package_bundle(result_path, bundle, structure=bad)


def test_package_with_extras_remains_byte_deterministic(tmp_path: Path) -> None:
    """Two packages with the same inputs (extras included) → identical bytes."""
    result_path = tmp_path / "result.json"
    _write_minimal_result(result_path)
    readme = tmp_path / "README.md"
    readme.write_text("# x\n", encoding="utf-8")

    a = tmp_path / "a.tar.gz"
    b = tmp_path / "b.tar.gz"
    package_bundle(result_path, a, readme=readme)
    package_bundle(result_path, b, readme=readme)
    assert a.read_bytes() == b.read_bytes()


def test_read_bundle_meta_rejects_missing_file(tmp_path: Path) -> None:
    with pytest.raises(PackageError, match="does not exist"):
        read_bundle_meta(tmp_path / "nope.tar.gz")


def test_read_bundle_meta_rejects_non_gzip(tmp_path: Path) -> None:
    bad = tmp_path / "claim.tar.gz"
    bad.write_bytes(b"this is plainly not gzipped data")
    with pytest.raises(PackageError, match="not a valid gzip"):
        read_bundle_meta(bad)


def test_read_bundle_meta_rejects_truncated_tar(tmp_path: Path) -> None:
    """A valid gzip stream containing junk bytes should produce
    a clean PackageError, not a raw TarError."""
    bad = tmp_path / "claim.tar.gz"
    with gzip.open(bad, "wb") as f:
        f.write(b"definitely not a tar stream")
    with pytest.raises(PackageError, match="not a valid tar"):
        read_bundle_meta(bad)
