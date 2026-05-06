"""Smoke tests for the bench_kit package itself."""

from __future__ import annotations

import re

import bench_kit


def test_version_is_pep440() -> None:
    assert re.match(r"^\d+\.\d+\.\d+", bench_kit.__version__)


def test_spec_version_matches_format() -> None:
    assert re.match(r"^\d+\.\d+\.\d+$", bench_kit.SPEC_VERSION)
