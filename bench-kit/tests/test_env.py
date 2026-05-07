"""Tests for the auto-collected ``env`` block."""

from __future__ import annotations

import re

from jsonschema import Draft202012Validator

from bench_kit.env import collect_env
from bench_kit.schemas import load_schema


def test_collect_env_conforms_to_schema() -> None:
    """The collected env must validate against result.schema.json's Env shape."""
    schema = load_schema("result")
    env_schema = schema["$defs"]["Env"]
    Draft202012Validator.check_schema(env_schema)
    Draft202012Validator(env_schema).validate(collect_env())


def test_collect_env_redacts_hostname() -> None:
    assert collect_env()["hostname_redacted"] is True


def test_collect_env_runtime_hash_is_sha256() -> None:
    assert re.match(r"^sha256:[0-9a-f]{64}$", collect_env()["runtime_hash"])


def test_collect_env_is_deterministic_for_same_source() -> None:
    """Calling twice with no source change must produce the same runtime_hash."""
    a = collect_env()["runtime_hash"]
    b = collect_env()["runtime_hash"]
    assert a == b
