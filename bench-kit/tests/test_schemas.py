"""Validate the JSON schemas themselves and the example fixtures.

These are the spec's load-bearing tests: if either schema is malformed
or the example fixtures drift out of compliance, CI fails before any
runtime code can ship.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from bench_kit.schemas import load_schema
from jsonschema import Draft202012Validator

_FIXTURE_DIR = Path(__file__).parent / "fixtures"


@pytest.mark.parametrize("schema_name", ["result", "meta"])
def test_schema_is_valid_jsonschema(schema_name: str) -> None:
    """Each schema must itself conform to JSON Schema Draft 2020-12."""
    schema = load_schema(schema_name)
    Draft202012Validator.check_schema(schema)


def test_result_schema_top_level_required_fields() -> None:
    """Lock in the top-level required fields so accidental removal is caught."""
    schema = load_schema("result")
    expected = {
        "schema_version",
        "battle_id",
        "battle_repo",
        "battle_commit",
        "ran_at",
        "env",
        "tools",
        "runs",
        "summary",
    }
    assert set(schema["required"]) == expected


def test_meta_schema_top_level_required_fields() -> None:
    """Lock in the top-level required fields for meta.yaml."""
    schema = load_schema("meta")
    expected = {
        "schema_version",
        "battle_id",
        "slug",
        "kind",
        "title",
        "tags",
        "bench_kit_version",
        "tasks_dir",
        "runners_dir",
        "fixtures",
        "tolerances",
    }
    assert set(schema["required"]) == expected


def test_example_result_fixture_validates() -> None:
    schema = load_schema("result")
    with (_FIXTURE_DIR / "result_minimal.json").open(encoding="utf-8") as f:
        data: dict[str, Any] = json.load(f)
    Draft202012Validator(schema).validate(data)


def test_example_meta_fixture_validates() -> None:
    schema = load_schema("meta")
    with (_FIXTURE_DIR / "meta_minimal.yaml").open(encoding="utf-8") as f:
        data: dict[str, Any] = yaml.safe_load(f)
    Draft202012Validator(schema).validate(data)


def test_result_rejects_unpinned_image() -> None:
    """A tag-based image reference must fail validation."""
    schema = load_schema("result")
    with (_FIXTURE_DIR / "result_minimal.json").open(encoding="utf-8") as f:
        data: dict[str, Any] = json.load(f)
    data["tools"][0]["image_digest"] = "browserless/chrome:latest"
    validator = Draft202012Validator(schema)
    errors = list(validator.iter_errors(data))
    assert errors, "expected validation error for non-digest image reference"


def test_meta_rejects_loose_tolerance() -> None:
    """Loosening a tolerance beyond bench-kit defaults must fail."""
    schema = load_schema("meta")
    with (_FIXTURE_DIR / "meta_minimal.yaml").open(encoding="utf-8") as f:
        data: dict[str, Any] = yaml.safe_load(f)
    data["tolerances"]["wall_clock_ms_p50"] = 0.99  # absurdly loose
    validator = Draft202012Validator(schema)
    errors = list(validator.iter_errors(data))
    assert errors, "expected validation error for loosened tolerance"


def test_meta_rejects_nonzero_success_rate_tolerance() -> None:
    """success_rate must always have tolerance exactly 0."""
    schema = load_schema("meta")
    with (_FIXTURE_DIR / "meta_minimal.yaml").open(encoding="utf-8") as f:
        data: dict[str, Any] = yaml.safe_load(f)
    data["tolerances"]["success_rate"] = 0.05
    validator = Draft202012Validator(schema)
    errors = list(validator.iter_errors(data))
    assert errors, "expected validation error for non-zero success_rate tolerance"


def test_meta_rejects_wrong_kind() -> None:
    """Only ``containerised`` is accepted; the framework is scoped to that class."""
    schema = load_schema("meta")
    with (_FIXTURE_DIR / "meta_minimal.yaml").open(encoding="utf-8") as f:
        data: dict[str, Any] = yaml.safe_load(f)
    data["kind"] = "proxy-execution"
    validator = Draft202012Validator(schema)
    errors = list(validator.iter_errors(data))
    assert errors, "expected validation error for non-containerised kind"
