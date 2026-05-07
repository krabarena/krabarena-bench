"""Orchestrator for static validation of a Battle directory.

Combines schema validation (``meta.yaml`` and ``tasks/*.yaml``),
runner linting, and ``fixtures/compose.yml`` policy checks into a
single :class:`ValidationReport`. ``bench validate`` calls this
function and CI rejects any report with issues.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version

from bench_kit import __version__ as _BENCH_KIT_VERSION
from bench_kit.lint import IMAGE_DIGEST_RE, LintIssue, lint_runners_dir
from bench_kit.schemas import load_schema


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
            LintIssue(path=battle_dir, code="BK040", message="battle directory does not exist")
        )
        return ValidationReport(battle_dir=battle_dir, issues=issues)

    meta_path = battle_dir / "meta.yaml"
    meta_data = _validate_meta(meta_path, battle_dir, issues)

    tasks_dir = _resolve_in_battle(battle_dir, (meta_data or {}).get("tasks_dir", "tasks"), issues)
    task_data_by_id = _validate_tasks(tasks_dir, issues) if tasks_dir is not None else {}

    runners_dir = _resolve_in_battle(
        battle_dir, (meta_data or {}).get("runners_dir", "runners"), issues
    )
    if runners_dir is not None:
        issues.extend(lint_runners_dir(runners_dir))

    compose_rel = (meta_data or {}).get("fixtures", {}).get("compose", "fixtures/compose.yml")
    compose_path = _resolve_in_battle(battle_dir, compose_rel, issues)
    fixture_services = _validate_compose(compose_path, issues) if compose_path is not None else None

    if fixture_services is not None and tasks_dir is not None and compose_path is not None:
        for task_id, task in task_data_by_id.items():
            svc = task.get("fixture_service")
            if isinstance(svc, str) and svc not in fixture_services:
                issues.append(
                    LintIssue(
                        path=tasks_dir / f"{task_id}.yaml",
                        code="BK022",
                        message=(
                            f"fixture_service {svc!r} not declared in "
                            f"{compose_path.relative_to(battle_dir)}"
                        ),
                    )
                )

    return ValidationReport(battle_dir=battle_dir, issues=issues)


def _resolve_in_battle(
    battle_dir: Path,
    relative: object,
    issues: list[LintIssue],
) -> Path | None:
    """Resolve ``relative`` against ``battle_dir`` and require containment.

    A malicious or accidental ``meta.yaml`` could point ``tasks_dir``,
    ``runners_dir`` or ``fixtures.compose`` at a path containing ``..``
    and escape the battle directory; ``bench validate`` would then read
    and validate files outside the battle. Reject anything whose
    resolved form is not within ``battle_dir``.
    """
    if not isinstance(relative, str):
        issues.append(
            LintIssue(
                path=battle_dir,
                code="BK041",
                message=f"path must be a string, got {type(relative).__name__}",
            )
        )
        return None
    candidate = (battle_dir / relative).resolve()
    if candidate != battle_dir and battle_dir not in candidate.parents:
        issues.append(
            LintIssue(
                path=battle_dir,
                code="BK041",
                message=(
                    f"path {relative!r} resolves outside the battle directory; "
                    f"`..` segments and absolute paths are not allowed"
                ),
            )
        )
        return None
    return candidate


def _load_yaml_mapping(
    path: Path,
    code: str,
    missing_msg: str,
    issues: list[LintIssue],
) -> dict[str, Any] | None:
    """Load ``path`` as a YAML mapping, recording any failure as an issue.

    Returns ``None`` if the file is missing, malformed, or whose root
    is not a mapping. The three failure modes share the issue ``code``
    so callers can scope all "this YAML file is unusable" reports under
    one stable identifier (BK010 / BK020 / BK030).
    """
    if not path.is_file():
        issues.append(LintIssue(path=path, code=code, message=missing_msg))
        return None
    try:
        with path.open(encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except yaml.YAMLError as exc:
        issues.append(LintIssue(path=path, code=code, message=f"YAML error: {exc}"))
        return None
    if not isinstance(data, dict):
        issues.append(
            LintIssue(
                path=path,
                code=code,
                message="must be a YAML mapping at the top level",
            )
        )
        return None
    return data


def _emit_schema_errors(
    path: Path,
    validator: Draft202012Validator,
    data: dict[str, Any],
    code: str,
    issues: list[LintIssue],
) -> None:
    """Append one :class:`LintIssue` per JSON Schema violation."""

    def _key(err: ValidationError) -> tuple[Any, ...]:
        return tuple(err.absolute_path)

    for err in sorted(validator.iter_errors(data), key=_key):
        loc = "/".join(str(p) for p in err.absolute_path) or "<root>"
        issues.append(
            LintIssue(
                path=path,
                code=code,
                message=f"schema violation at {loc}: {err.message}",
            )
        )


def _validate_meta(
    meta_path: Path,
    battle_dir: Path,
    issues: list[LintIssue],
) -> dict[str, Any] | None:
    data = _load_yaml_mapping(meta_path, "BK010", "meta.yaml is missing", issues)
    if data is None:
        return None

    validator = Draft202012Validator(load_schema("meta"))
    _emit_schema_errors(meta_path, validator, data, "BK010", issues)

    if isinstance(data.get("slug"), str) and data["slug"] != battle_dir.name:
        issues.append(
            LintIssue(
                path=meta_path,
                code="BK011",
                message=(
                    f"meta.slug={data['slug']!r} does not match directory name {battle_dir.name!r}"
                ),
            )
        )

    spec_str = data.get("bench_kit_version")
    if isinstance(spec_str, str):
        _check_bench_kit_compatibility(meta_path, spec_str, issues)
    return data


def _check_bench_kit_compatibility(
    meta_path: Path,
    spec_str: str,
    issues: list[LintIssue],
) -> None:
    """Per SPEC §8, refuse to validate a Battle whose declared
    ``bench_kit_version`` range does not include the running version.

    Compares against the *release* portion of ``bench_kit.__version__``
    so a dev pre-release (``0.1.0.dev0``) of an in-range version
    satisfies a range like ``>=0.1.0,<0.2.0`` — we do not want every
    dev install to fail validation against a published Battle.
    """
    try:
        specifier = SpecifierSet(spec_str)
    except InvalidSpecifier as exc:
        issues.append(
            LintIssue(
                path=meta_path,
                code="BK012",
                message=f"bench_kit_version is not a valid PEP 440 specifier: {exc}",
            )
        )
        return
    try:
        running = Version(_BENCH_KIT_VERSION)
    except InvalidVersion:
        return
    release = Version(".".join(str(p) for p in running.release))
    if release not in specifier:
        issues.append(
            LintIssue(
                path=meta_path,
                code="BK012",
                message=(
                    f"running bench-kit {_BENCH_KIT_VERSION} does not satisfy "
                    f"bench_kit_version={spec_str!r}; install a compatible version"
                ),
            )
        )


def _validate_tasks(
    tasks_dir: Path,
    issues: list[LintIssue],
) -> dict[str, dict[str, Any]]:
    """Validate every ``*.yaml`` in ``tasks_dir`` and return them by id."""
    out: dict[str, dict[str, Any]] = {}
    if not tasks_dir.is_dir():
        issues.append(
            LintIssue(path=tasks_dir, code="BK020", message="tasks directory does not exist")
        )
        return out

    validator = Draft202012Validator(load_schema("task"))

    yaml_files = sorted(p for p in tasks_dir.glob("*.yaml") if not p.name.startswith("_"))
    for path in yaml_files:
        data = _load_yaml_mapping(path, "BK020", f"{path.name} is missing", issues)
        if data is None:
            continue
        _emit_schema_errors(path, validator, data, "BK020", issues)
        if isinstance(data.get("id"), str) and data["id"] != path.stem:
            issues.append(
                LintIssue(
                    path=path,
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
    compose = _load_yaml_mapping(compose_path, "BK030", "compose file is missing", issues)
    if compose is None:
        return None

    internal_networks = _check_compose_networks(compose_path, compose, issues)

    services = compose.get("services")
    if not isinstance(services, dict) or not services:
        issues.append(
            LintIssue(
                path=compose_path,
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
        issues.extend(_check_compose_service(compose_path, svc_name, svc_cfg, internal_networks))
    return service_names


def _check_compose_networks(
    compose_path: Path,
    compose: dict[str, Any],
    issues: list[LintIssue],
) -> set[str]:
    """Validate the ``networks:`` block and return the set of internal network names.

    A "network is internal" iff it is declared with ``internal: true``.
    Returning the set lets ``_check_compose_service`` enforce that
    every service's ``networks:`` membership is a subset of it —
    catching services that silently fall onto Compose's default
    egress-enabled bridge network.
    """
    internal: set[str] = set()
    networks = compose.get("networks")
    if not isinstance(networks, dict) or not networks:
        issues.append(
            LintIssue(
                path=compose_path,
                code="BK031",
                message=(
                    "compose file must declare at least one network with `internal: true`; "
                    "fixture services may not have egress"
                ),
            )
        )
        return internal
    for net_name, net_cfg in networks.items():
        if not isinstance(net_name, str):
            continue
        if isinstance(net_cfg, dict) and net_cfg.get("internal", False):
            internal.add(net_name)
        else:
            issues.append(
                LintIssue(
                    path=compose_path,
                    code="BK031",
                    message=(
                        f"network {net_name!r} must declare `internal: true` to "
                        f"prevent egress from fixture services"
                    ),
                )
            )
    return internal


def _check_compose_service(
    compose_path: Path,
    name: str,
    cfg: object,
    internal_networks: set[str],
) -> list[LintIssue]:
    """Validate a single service entry in compose.yml."""
    issues: list[LintIssue] = []
    if not isinstance(cfg, dict):
        issues.append(
            LintIssue(
                path=compose_path,
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
                code="BK032",
                message=(
                    f"service {name!r} must declare either `image:` "
                    f"(digest-pinned) or `build:` (local path)"
                ),
            )
        )
    if has_image:
        image = cfg["image"]
        if not (isinstance(image, str) and IMAGE_DIGEST_RE.match(image)):
            issues.append(
                LintIssue(
                    path=compose_path,
                    code="BK033",
                    message=(
                        f"service {name!r} image {image!r} must be sha256-pinned: "
                        f"`<repo>@sha256:<64-hex>`"
                    ),
                )
            )
    issues.extend(_check_service_network_isolation(compose_path, name, cfg, internal_networks))
    return issues


def _check_service_network_isolation(
    compose_path: Path,
    name: str,
    cfg: dict[str, Any],
    internal_networks: set[str],
) -> list[LintIssue]:
    """Reject services that can reach the host network or Compose's default bridge.

    Containment requires three things: (1) no ``network_mode`` (any value
    bypasses the per-network isolation Compose builds for us); (2) an
    explicit ``networks:`` member, otherwise Compose attaches the
    service to its default bridge with egress; (3) every referenced
    network is one of the ``internal: true`` ones declared at the top
    of the file.
    """
    issues: list[LintIssue] = []
    if "network_mode" in cfg:
        issues.append(
            LintIssue(
                path=compose_path,
                code="BK034",
                message=(
                    f"service {name!r} declares `network_mode`, which bypasses "
                    f"the per-network isolation required by SPEC §6"
                ),
            )
        )
    raw = cfg.get("networks")
    if raw is None:
        issues.append(
            LintIssue(
                path=compose_path,
                code="BK034",
                message=(
                    f"service {name!r} must declare `networks:` listing only "
                    f"`internal: true` networks; without it Compose attaches "
                    f"the default egress-enabled bridge"
                ),
            )
        )
        return issues
    if isinstance(raw, list | dict):
        referenced = [str(n) for n in raw if isinstance(n, str)]
    else:
        issues.append(
            LintIssue(
                path=compose_path,
                code="BK034",
                message=f"service {name!r}: `networks:` must be a list or mapping",
            )
        )
        return issues
    if not referenced:
        issues.append(
            LintIssue(
                path=compose_path,
                code="BK034",
                message=f"service {name!r}: `networks:` must reference at least one network",
            )
        )
    for net in referenced:
        if net not in internal_networks:
            issues.append(
                LintIssue(
                    path=compose_path,
                    code="BK034",
                    message=(
                        f"service {name!r} references non-internal network {net!r}; "
                        f"only `internal: true` networks are allowed"
                    ),
                )
            )
    return issues


__all__ = ["ValidationReport", "validate_battle"]
