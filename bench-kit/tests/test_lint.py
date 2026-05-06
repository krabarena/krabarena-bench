"""Tests for AST-based runner module linting."""

from __future__ import annotations

from pathlib import Path

import pytest

from bench_kit.lint import lint_runner_file, lint_runners_dir

_VALID_DIGEST = "lib/img@sha256:" + "0" * 64

_CLEAN_RUNNER = f'''
"""A minimal but compliant runner."""

from __future__ import annotations

from bench_kit.runner_base import Runner, RunResult, Task


class FakeRunner(Runner):
    name = "fake"
    image = "{_VALID_DIGEST}"

    def run(self, task: Task) -> RunResult:
        raise NotImplementedError
'''


def _write(tmp_path: Path, name: str, body: str) -> Path:
    f = tmp_path / name
    f.write_text(body, encoding="utf-8")
    return f


def test_clean_runner_has_no_issues(tmp_path: Path) -> None:
    f = _write(tmp_path, "fake.py", _CLEAN_RUNNER)
    assert lint_runner_file(f) == []


@pytest.mark.parametrize(
    "import_line",
    [
        "import subprocess",
        "import socket",
        "import requests",
        "import urllib.request",
        "import httpx",
        "from docker import from_env",
        "from importlib import import_module",
    ],
)
def test_forbidden_imports_rejected(tmp_path: Path, import_line: str) -> None:
    body = f"{import_line}\n" + _CLEAN_RUNNER
    f = _write(tmp_path, "bad.py", body)
    issues = lint_runner_file(f)
    assert any(i.code == "BK001" for i in issues), issues


@pytest.mark.parametrize(
    ("body_extra", "expect"),
    [
        ("import os\nos.system('echo hi')\n", "BK002"),
        ("import os\nos.popen('ls')\n", "BK002"),
        ("eval('1+1')\n", "BK002"),
        ("exec('x=1')\n", "BK002"),
        ("__import__('socket')\n", "BK002"),
    ],
)
def test_forbidden_calls_rejected(tmp_path: Path, body_extra: str, expect: str) -> None:
    body = body_extra + _CLEAN_RUNNER
    f = _write(tmp_path, "bad.py", body)
    issues = lint_runner_file(f)
    assert any(i.code == expect for i in issues), issues


def test_unpinned_image_rejected(tmp_path: Path) -> None:
    body = _CLEAN_RUNNER.replace(_VALID_DIGEST, "lib/img:latest")
    f = _write(tmp_path, "bad.py", body)
    issues = lint_runner_file(f)
    assert any(i.code == "BK003" for i in issues), issues


def test_missing_image_attr_rejected(tmp_path: Path) -> None:
    body = """
from bench_kit.runner_base import Runner

class FakeRunner(Runner):
    name = "fake"

    def run(self, task):
        return None
"""
    f = _write(tmp_path, "bad.py", body)
    issues = lint_runner_file(f)
    assert any(i.code == "BK004" for i in issues), issues


def test_syntax_error_reported(tmp_path: Path) -> None:
    f = _write(tmp_path, "bad.py", "def !! oops\n")
    issues = lint_runner_file(f)
    assert any(i.code == "BK005" for i in issues), issues


def test_underscore_files_skipped(tmp_path: Path) -> None:
    # A file with violations but a leading underscore must be ignored
    # by the directory linter (it is treated as scaffolding/internal).
    _write(tmp_path, "_helper.py", "import subprocess\n")
    _write(tmp_path, "fake.py", _CLEAN_RUNNER)
    assert lint_runners_dir(tmp_path) == []


def test_runners_dir_missing_reported(tmp_path: Path) -> None:
    issues = lint_runners_dir(tmp_path / "nope")
    assert any(i.code == "BK006" for i in issues), issues
