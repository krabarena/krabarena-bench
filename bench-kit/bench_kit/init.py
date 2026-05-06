"""Scaffold a new containerised Battle directory from inline templates.

Implements ``bench init battle <topic> --battle-id <uuid>``. Refuses to
overwrite an existing non-empty directory; declines to write outside
the current working directory tree.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from pathlib import Path

from bench_kit import SPEC_VERSION

_SLUG_RE = re.compile(r"^[a-z][a-z0-9-]*$")
_SLUG_MIN = 3
_SLUG_MAX = 80


class InitError(Exception):
    """Raised when scaffolding cannot proceed."""


@dataclass(frozen=True, slots=True)
class InitPlan:
    """Resolved plan for ``bench init battle`` — files to create."""

    target_dir: Path
    files: dict[Path, str]


_META_TEMPLATE = """\
schema_version: "{spec_version}"
battle_id: "{battle_id}"
slug: "{slug}"
kind: "containerised"
title: "TODO — fill in the Battle's central question"
tags:
  - todo
bench_kit_version: ">=0.1.0,<0.2.0"
tasks_dir: "tasks"
runners_dir: "runners"
fixtures:
  compose: "fixtures/compose.yml"
metrics:
  optional: []
tolerances:
  success_rate: 0.0
  wall_clock_ms_p50: 0.20
  wall_clock_ms_p95: 0.30
  peak_rss_mb: 0.25
"""

_README_TEMPLATE = """\
# {slug}

> **Battle on KrabArena:** <https://krabarena.org/battles/{slug}>

One-paragraph description of the central question this Battle answers.

## Tools compared

- TODO

## Running locally

```sh
./run.sh
```

See [`../../docs/AUTHORING_BATTLES.md`](../../docs/AUTHORING_BATTLES.md)
for the full workflow.
"""

_TASK_TEMPLATE = """\
schema_version: "{spec_version}"
id: example
title: "Example task — replace me"
description: |
  Describe what this task asks every tool to do, and what counts as
  a successful run. Tasks must be deterministic, internet-free, and
  use only the fixture services declared in fixtures/compose.yml.
fixture_service: example
inputs:
  target_count: 100
expected:
  count: 100
success_predicate: "len(output.items) == 100"
timeout_s: 60
iterations: 5
limits:
  memory_mb: 2048
  cpus: 2
"""

_PLACEHOLDER_DIGEST = "0" * 64

_RUNNER_TEMPLATE = '''\
"""Example runner — replace with a real one and pin a real digest."""

from __future__ import annotations

from bench_kit.exec import run_constrained
from bench_kit.runner_base import Runner, RunResult, Task


class ExampleRunner(Runner):
    name = "example"
    # MUST be sha256-pinned — never use a tag in production runners.
    image = "library/hello-world@sha256:{digest}"

    def run(self, task: Task) -> RunResult:
        return run_constrained(
            image=self.image,
            args=["echo", task.id],
            network=task.fixture_network,
            mounts={{"task": task.dir, "results": task.results_dir}},
            limits=task.limits,
        )
'''

_COMPOSE_TEMPLATE = """\
# Local fixtures for the {slug} Battle.
# All networks must be `internal: true` — fixtures never have egress.

services:
  example:
    # Replace with a digest-pinned image or a local build dir.
    image: library/hello-world@sha256:{digest}
    networks: [bench]

networks:
  bench:
    internal: true
"""

_DOCKERFILE_TEMPLATE = """\
# Optional harness image — only needed if your runners require a
# specific Python/Node toolchain to drive the upstream containers.
# Most Battles can leave this as-is and run bench-kit on the host.

FROM python:3.12-slim
WORKDIR /work
"""


def _validate_slug(slug: str) -> None:
    if not _SLUG_MIN <= len(slug) <= _SLUG_MAX:
        msg = f"slug must be {_SLUG_MIN}-{_SLUG_MAX} chars, got {len(slug)}"
        raise InitError(msg)
    if not _SLUG_RE.match(slug):
        msg = f"slug must match {_SLUG_RE.pattern!r}, got {slug!r}"
        raise InitError(msg)


def _validate_battle_id(battle_id: str) -> None:
    try:
        parsed = uuid.UUID(battle_id)
    except ValueError as exc:
        msg = f"battle_id must be a valid UUID, got {battle_id!r}"
        raise InitError(msg) from exc
    # Reject the all-zeros UUID — it almost certainly means a placeholder
    # leaked through.
    if parsed.int == 0:
        msg = "battle_id must not be the nil UUID"
        raise InitError(msg)
    # Require canonical hyphenated form. ``uuid.UUID`` happily parses
    # 32-hex-no-hyphens, but every other tool in the toolchain
    # (krabarena.org URLs, Postgres, JSON Schema "uuid" format) wants
    # the canonical form, so reject the unhyphenated input here.
    if str(parsed) != battle_id.lower():
        msg = f"battle_id must be in canonical 8-4-4-4-12 hyphenated form, got {battle_id!r}"
        raise InitError(msg)


def plan(slug: str, battle_id: str, parent_dir: Path) -> InitPlan:
    """Compute the scaffold plan without touching the filesystem."""
    _validate_slug(slug)
    _validate_battle_id(battle_id)

    target = parent_dir / "battles" / slug
    files: dict[Path, str] = {
        Path("meta.yaml"): _META_TEMPLATE.format(
            spec_version=SPEC_VERSION, battle_id=battle_id, slug=slug
        ),
        Path("README.md"): _README_TEMPLATE.format(slug=slug),
        Path("tasks/example.yaml"): _TASK_TEMPLATE.format(spec_version=SPEC_VERSION),
        Path("runners/example.py"): _RUNNER_TEMPLATE.format(digest=_PLACEHOLDER_DIGEST),
        Path("fixtures/compose.yml"): _COMPOSE_TEMPLATE.format(
            slug=slug, digest=_PLACEHOLDER_DIGEST
        ),
        Path("Dockerfile"): _DOCKERFILE_TEMPLATE,
    }
    return InitPlan(target_dir=target, files=files)


def apply_plan(p: InitPlan) -> None:
    """Materialise an :class:`InitPlan` on disk."""
    if p.target_dir.exists() and any(p.target_dir.iterdir()):
        msg = f"refusing to overwrite non-empty directory {p.target_dir}"
        raise InitError(msg)
    p.target_dir.mkdir(parents=True, exist_ok=True)
    for rel, content in p.files.items():
        full = p.target_dir / rel
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(content, encoding="utf-8")


def init_battle(slug: str, battle_id: str, parent_dir: Path | None = None) -> Path:
    """Scaffold ``battles/<slug>/`` under ``parent_dir`` (default: cwd).

    Returns the resolved target directory. Raises :class:`InitError`
    on any validation failure or if the target already has content.
    """
    parent = (parent_dir or Path.cwd()).resolve()
    p = plan(slug, battle_id, parent)
    apply_plan(p)
    return p.target_dir


__all__ = ["InitError", "InitPlan", "apply_plan", "init_battle", "plan"]
