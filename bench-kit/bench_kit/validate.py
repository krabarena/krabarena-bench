"""Orchestrator for static validation of a Battle directory.

Combines schema validation (``meta.yaml`` and ``tasks/*.yaml``),
runner linting, and ``fixtures/compose.yml`` policy checks into a
single :class:`ValidationReport`. ``bench validate`` calls this
function and CI rejects any report with issues.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator

from bench_kit.lint import LintIssue, lint_runners_dir
from bench_kit.schemas import load_schema

_IMAGE_DIGEST_RE = re.compile(
    r"^[a-z0-9][a-z0-9._/-]*@sha256:[0-9a-f]{64}$",
    flags=re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class ValidationReport:
    """Aggregate result of validating a Battle directory."""

    battle_dir: Path
    issues: list[LintIssue]

    @property
    def ok(self) -> bool:
        return not self.issues


def validate_battle(battle_dir: Path) -> ValidationReport:
    """Run every static check against ``battle_dir``.

    Errors are reported as :class:`LintIssue` records with codes in
    the ``BK###`` family. The function does not stop at the first
    failure — it collects everything so a single CI run gives the
    author the full picture.
    """
    issues: list[LintIssue] = []
    battle_dir = battle_dir.resolve()

    if not battle_dir.is_dir():
        issues.append(
            LintIssue(
                path=battle_dir,
                line=0,
                col=0,
                code="BK040",
                message="battle directory does not exist",
            )
        )
        return ValidationReport(battle_dir=battle_dir, issues=issues)

    meta_path = battle_dir / "meta.yaml"
    meta_data = _validate_meta(meta_path, battle_dir, issues)

    tasks_dir_name = (meta_data or {}).get("tasks_dir", "tasks")
    tasks_dir = battle_dir / str(tasks_dir_name)
    task_data_by_id = _validate_tasks(tasks_dir, issues)

    runners_dir_name = (meta_data or {}).get("runners_dir", "runners")
    runners_dir = battle_dir / str(runners_dir_name)
    issues.extend(lint_runners_dir(runners_dir))

    compose_rel = (meta_data or {}).get("fixtures", {}).get("compose", "fixtures/compose.yml")
    compose_path = battle_dir / str(compose_rel)
    fixture_services = _validate_compose(compose_path, issues)

    # Cross-check tasks reference services that actually exist.
    if fixture_services is not None:
        for task_id, task in task_data_by_id.items():
            svc = task.get("fixture_service")
            if isinstance(svc, str) and svc not in fixture_services:
                issues.append(
                    LintIssue(
                        path=tasks_dir / f"{task_id}.yaml",
                        line=0,
                        col=0,
                        code="BK022",
                        message=(
                            f"fixture_service {svc!r} not declared in "
                            f"{compose_path.relative_to(battle_dir)}"
                        ),
                    )
                )

    return ValidationReport(battle_dir=battle_dir, issues=issues)


def _validate_meta(
    meta_path: Path,
    battle_dir: Path,
    issues: list[LintIssue],
) -> dict[str, Any] | None:
    if not meta_path.is_file():
        issues.append(
            LintIssue(
                path=meta_path,
                line=0,
                col=0,
                code="BK010",
                message="meta.yaml is missing",
            )
        )
        return None
    try:
        with meta_path.open(encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except yaml.YAMLError as exc:
        issues.append(
            LintIssue(path=meta_path, line=0, col=0, code="BK010", message=f"YAML error: {exc}")
        )
        return None
    if not isinstance(data, dict):
        issues.append(
            LintIssue(
                path=meta_path,
                line=0,
                col=0,
                code="BK010",
                message="meta.yaml must be a YAML mapping at the top level",
            )
        )
        return None

    schema = load_schema("meta")
    validator = Draft202012Validator(schema)
    for err in sorted(validator.iter_errors(data), key=lambda e: e.absolute_path):
        loc = "/".join(str(p) for p in err.absolute_path) or "<root>"
        issues.append(
            LintIssue(
                path=meta_path,
                line=0,
                col=0,
                code="BK010",
                message=f"schema violation at {loc}: {err.message}",
            )
        )

    # Slug coherence: meta.slug should match the directory name.
    if isinstance(data.get("slug"), str) and data["slug"] != battle_dir.name:
        issues.append(
            LintIssue(
                path=meta_path,
                line=0,
                col=0,
                code="BK011",
                message=(
                    f"meta.slug={data['slug']!r} does not match directory name {battle_dir.name!r}"
                ),
            )
        )
    return data


def _validate_tasks(
    tasks_dir: Path,
    issues: list[LintIssue],
) -> dict[str, dict[str, Any]]:
    """Validate every ``*.yaml`` in ``tasks_dir`` and return them by id."""
    out: dict[str, dict[str, Any]] = {}
    if not tasks_dir.is_dir():
        issues.append(
            LintIssue(
                path=tasks_dir,
                line=0,
                col=0,
                code="BK020",
                message="tasks directory does not exist",
            )
        )
        return out

    schema = load_schema("task")
    validator = Draft202012Validator(schema)

    yaml_files = sorted(p for p in tasks_dir.glob("*.yaml") if not p.name.startswith("_"))
    for path in yaml_files:
        try:
            with path.open(encoding="utf-8") as f:
                data = yaml.safe_load(f)
        except yaml.YAMLError as exc:
            issues.append(
                LintIssue(path=path, line=0, col=0, code="BK020", message=f"YAML error: {exc}")
            )
            continue
        if not isinstance(data, dict):
            issues.append(
                LintIssue(
                    path=path,
                    line=0,
                    col=0,
                    code="BK020",
                    message="task YAML must be a mapping at the top level",
                )
            )
            continue
        for err in sorted(validator.iter_errors(data), key=lambda e: e.absolute_path):
            loc = "/".join(str(p) for p in err.absolute_path) or "<root>"
            issues.append(
                LintIssue(
                    path=path,
                    line=0,
                    col=0,
                    code="BK020",
                    message=f"schema violation at {loc}: {err.message}",
                )
            )
        # id must match the filename stem.
        if isinstance(data.get("id"), str) and data["id"] != path.stem:
            issues.append(
                LintIssue(
                    path=path,
                    line=0,
                    col=0,
                    code="BK021",
                    message=(f"task id {data['id']!r} does not match filename stem {path.stem!r}"),
                )
            )
        if isinstance(data.get("id"), str):
            out[data["id"]] = data
    return out


def _validate_compose(
    compose_path: Path,
    issues: list[LintIssue],
) -> set[str] | None:
    """Validate ``fixtures/compose.yml`` and return its service names."""
    if not compose_path.is_file():
        issues.append(
            LintIssue(
                path=compose_path,
                line=0,
                col=0,
                code="BK030",
                message="compose file is missing",
            )
        )
        return None
    try:
        with compose_path.open(encoding="utf-8") as f:
            compose = yaml.safe_load(f)
    except yaml.YAMLError as exc:
        issues.append(
            LintIssue(path=compose_path, line=0, col=0, code="BK030", message=f"YAML error: {exc}")
        )
        return None
    if not isinstance(compose, dict):
        issues.append(
            LintIssue(
                path=compose_path,
                line=0,
                col=0,
                code="BK030",
                message="compose file must be a YAML mapping at the top level",
            )
        )
        return None

    networks = compose.get("networks")
    if not isinstance(networks, dict) or not networks:
        issues.append(
            LintIssue(
                path=compose_path,
                line=0,
                col=0,
                code="BK031",
                message=(
                    "compose file must declare at least one network with `internal: true`; "
                    "fixture services may not have egress"
                ),
            )
        )
    else:
        for net_name, net_cfg in networks.items():
            if not isinstance(net_cfg, dict) or not net_cfg.get("internal", False):
                issues.append(
                    LintIssue(
                        path=compose_path,
                        line=0,
                        col=0,
                        code="BK031",
                        message=(
                            f"network {net_name!r} must declare `internal: true` to "
                            f"prevent egress from fixture services"
                        ),
                    )
                )

    services = compose.get("services")
    if not isinstance(services, dict) or not services:
        issues.append(
            LintIssue(
                path=compose_path,
                line=0,
                col=0,
                code="BK032",
                message="compose file must declare at least one service",
            )
        )
        return set()

    service_names: set[str] = set()
    for svc_name, svc_cfg in services.items():
        if not isinstance(svc_name, str):
            continue
        service_names.add(svc_name)
        issues.extend(_check_compose_service(compose_path, svc_name, svc_cfg))
    return service_names


def _check_compose_service(
    compose_path: Path,
    name: str,
    cfg: object,
) -> list[LintIssue]:
    """Validate a single service entry in compose.yml."""
    issues: list[LintIssue] = []
    if not isinstance(cfg, dict):
        issues.append(
            LintIssue(
                path=compose_path,
                line=0,
                col=0,
                code="BK032",
                message=f"service {name!r} must be a mapping",
            )
        )
        return issues
    has_image = "image" in cfg
    has_build = "build" in cfg
    if not (has_image or has_build):
        issues.append(
            LintIssue(
                path=compose_path,
                line=0,
                col=0,
                code="BK032",
                message=(
                    f"service {name!r} must declare either `image:` "
                    f"(digest-pinned) or `build:` (local path)"
                ),
            )
        )
    if has_image:
        image = cfg["image"]
        if not (isinstance(image, str) and _IMAGE_DIGEST_RE.match(image)):
            issues.append(
                LintIssue(
                    path=compose_path,
                    line=0,
                    col=0,
                    code="BK033",
                    message=(
                        f"service {name!r} image {image!r} must be sha256-pinned: "
                        f"`<repo>@sha256:<64-hex>`"
                    ),
                )
            )
    return issues


__all__ = ["ValidationReport", "validate_battle"]
