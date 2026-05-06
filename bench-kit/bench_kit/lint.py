"""Static checks for ``runners/*.py`` modules.

Runners drive container processes during a benchmark; their attack
surface is the most consequential in the framework. We forbid any
direct path to the host (``subprocess``, ``socket``, HTTP libs, the
docker SDK) and require the upstream image reference to be pinned by
sha256 digest. See SPEC.md §5.1 for the rationale.

This module is AST-based and does not execute the runner — that is
intentional, because executing untrusted Python during static checks
would defeat the purpose.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path

# Forbidden top-level module names. ``from X import …`` and ``import X``
# both fail the check.
_FORBIDDEN_IMPORTS: frozenset[str] = frozenset(
    {
        "subprocess",
        "socket",
        "ssl",
        "requests",
        "urllib",
        "urllib2",
        "urllib3",
        "httpx",
        "aiohttp",
        "http",
        "docker",
        "importlib",
        "ctypes",
    }
)

# Forbidden attribute-call patterns of the form ``module.attr(...)``.
# Bare names like ``eval`` are also rejected via _FORBIDDEN_BUILTINS.
_FORBIDDEN_CALLS: frozenset[tuple[str, str]] = frozenset(
    {
        ("os", "system"),
        ("os", "popen"),
        ("os", "execv"),
        ("os", "execve"),
        ("os", "execvp"),
        ("os", "execvpe"),
        ("os", "spawnl"),
        ("os", "spawnv"),
        ("os", "spawnvp"),
    }
)

# Forbidden bare builtin calls.
_FORBIDDEN_BUILTINS: frozenset[str] = frozenset({"eval", "exec", "compile", "__import__", "open"})

# Acceptable image reference: ``<repo>[/<repo>...]@sha256:<64 hex>``.
# Re-used by ``validate`` for compose service images.
_IMAGE_DIGEST_RE = re.compile(
    r"^[a-z0-9][a-z0-9._/-]*@sha256:[0-9a-f]{64}$",
    flags=re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class LintIssue:
    """One static-check finding.

    Codes follow the ``BK###`` family so they can be referenced in
    PR review comments and stable across releases. ``line``/``col``
    default to ``0`` for findings without a meaningful source position
    (most file-level issues raised by ``validate``).
    """

    path: Path
    code: str
    message: str
    line: int = 0
    col: int = 0

    def __str__(self) -> str:
        if self.line or self.col:
            return f"{self.path}:{self.line}:{self.col}: {self.code} {self.message}"
        return f"{self.path}: {self.code} {self.message}"


def _root_module(name: str) -> str:
    """Return the top-level package of a dotted import name."""
    return name.split(".", 1)[0]


def _check_imports(tree: ast.AST, path: Path) -> list[LintIssue]:
    issues: list[LintIssue] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = _root_module(alias.name)
                if root in _FORBIDDEN_IMPORTS:
                    issues.append(
                        LintIssue(
                            path=path,
                            line=node.lineno,
                            col=node.col_offset,
                            code="BK001",
                            message=(
                                f"forbidden import {alias.name!r}: runners must use "
                                f"bench_kit helpers, not direct host I/O"
                            ),
                        )
                    )
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            root = _root_module(module)
            if root in _FORBIDDEN_IMPORTS:
                issues.append(
                    LintIssue(
                        path=path,
                        line=node.lineno,
                        col=node.col_offset,
                        code="BK001",
                        message=(
                            f"forbidden import from {module!r}: runners must use "
                            f"bench_kit helpers, not direct host I/O"
                        ),
                    )
                )
    return issues


def _check_calls(tree: ast.AST, path: Path) -> list[LintIssue]:
    issues: list[LintIssue] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
            pair = (func.value.id, func.attr)
            if pair in _FORBIDDEN_CALLS:
                issues.append(
                    LintIssue(
                        path=path,
                        line=node.lineno,
                        col=node.col_offset,
                        code="BK002",
                        message=(
                            f"forbidden call {pair[0]}.{pair[1]}(): "
                            f"runners must use bench_kit.exec for process launches"
                        ),
                    )
                )
        elif isinstance(func, ast.Name) and func.id in _FORBIDDEN_BUILTINS:
            issues.append(
                LintIssue(
                    path=path,
                    line=node.lineno,
                    col=node.col_offset,
                    code="BK002",
                    message=(
                        f"forbidden builtin {func.id}(): "
                        f"runners must not perform dynamic code execution or open "
                        f"host files directly"
                    ),
                )
            )
    return issues


def _check_image_attr(tree: ast.AST, path: Path) -> list[LintIssue]:
    """Find every Runner-like class and verify its ``image`` attribute is digest-pinned.

    A class is considered Runner-like if it has a class-level ``image``
    string assignment. We do not require subclassing ``Runner`` here
    because (a) the assignment alone is sufficient to identify the
    contract, and (b) handling all forms of class subclassing
    (qualified names, aliases, base-class chains) without resolution
    is brittle.
    """
    issues: list[LintIssue] = []
    found_any_image = False

    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        for stmt in node.body:
            target_name, value, lineno, col = _extract_assignment(stmt)
            if target_name != "image":
                continue
            found_any_image = True
            if not isinstance(value, ast.Constant) or not isinstance(value.value, str):
                issues.append(
                    LintIssue(
                        path=path,
                        line=lineno,
                        col=col,
                        code="BK003",
                        message="`image` attribute must be a string literal",
                    )
                )
                continue
            if not _IMAGE_DIGEST_RE.match(value.value):
                issues.append(
                    LintIssue(
                        path=path,
                        line=lineno,
                        col=col,
                        code="BK003",
                        message=(
                            f"`image` must be sha256-pinned (got {value.value!r}); "
                            f"use `<repo>@sha256:<64-hex>` — never a tag"
                        ),
                    )
                )

    if not found_any_image:
        issues.append(
            LintIssue(
                path=path,
                line=1,
                code="BK004",
                message=(
                    "no `image` attribute found on any class; every runner module "
                    "must define a Runner subclass with `image = '<repo>@sha256:...'`"
                ),
            )
        )
    return issues


def _extract_assignment(
    stmt: ast.stmt,
) -> tuple[str | None, ast.expr | None, int, int]:
    """Return ``(target_name, value, lineno, col)`` for a simple class-level
    assignment, or ``(None, None, 0, 0)`` if ``stmt`` is something else.

    Handles both ``image = "..."`` and ``image: ClassVar[str] = "..."``.
    """
    if isinstance(stmt, ast.Assign):
        if len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Name):
            return stmt.targets[0].id, stmt.value, stmt.lineno, stmt.col_offset
    elif isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
        return stmt.target.id, stmt.value, stmt.lineno, stmt.col_offset
    return None, None, 0, 0


def lint_runner_file(path: Path) -> list[LintIssue]:
    """Run all static checks on a single runner module."""
    try:
        source = path.read_text(encoding="utf-8")
    except OSError as exc:
        return [LintIssue(path=path, code="BK005", message=f"cannot read file: {exc}")]
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError as exc:
        return [
            LintIssue(
                path=path,
                line=exc.lineno or 0,
                col=exc.offset or 0,
                code="BK005",
                message=f"syntax error: {exc.msg}",
            )
        ]

    issues = _check_imports(tree, path)
    issues.extend(_check_calls(tree, path))
    issues.extend(_check_image_attr(tree, path))
    return sorted(issues, key=lambda i: (i.line, i.col, i.code))


def lint_runners_dir(runners_dir: Path) -> list[LintIssue]:
    """Lint every ``*.py`` file under ``runners_dir`` (non-recursive).

    Returns issues sorted by file path and source position. Empty
    list means clean.
    """
    issues: list[LintIssue] = []
    if not runners_dir.is_dir():
        return [
            LintIssue(path=runners_dir, code="BK006", message="runners directory does not exist")
        ]
    for py in sorted(runners_dir.glob("*.py")):
        # Leading-underscore files are scaffolding, not real runners; they are
        # excluded from `bench validate` but still subject to ruff/mypy.
        if py.name.startswith("_"):
            continue
        issues.extend(lint_runner_file(py))
    return issues


__all__ = ["LintIssue", "lint_runner_file", "lint_runners_dir"]
