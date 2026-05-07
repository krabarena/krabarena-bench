"""Bundle a ``result.json`` into a Claim artefact (``claim.tar.gz``).

The bundle is **pointer-only** — see SPEC §1 (vocabulary) and §3.1.
It contains the result, per-iteration logs, and a small ``meta.json``
header pointing at ``battle_repo + battle_commit``. It does **not**
contain Battle source code; verifiers fetch the same commit out of
``github.com/keenableai/krabarena-bench`` themselves.

This lets the artefact stay tiny (~hundreds of KB instead of MB), and
removes a class of supply-chain bugs where a Claimer could ship a
modified runner along with a result and mislead verifiers about what
was actually executed.
"""

from __future__ import annotations

import gzip
import io
import json
import tarfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

from bench_kit import __version__ as _BENCH_KIT_VERSION
from bench_kit.schemas import load_schema

_NIL_COMMIT = "0" * 40
_BUNDLE_VERSION = "0.1.0"


class PackageError(Exception):
    """Raised when a Claim bundle cannot be assembled."""


@dataclass(frozen=True, slots=True)
class BundleMeta:
    """Header written to ``meta.json`` inside the Claim bundle.

    Verifiers parse this to discover what to ``git clone`` and at
    which commit to reproduce the run; everything else (env, runs,
    summary) lives in the embedded ``result.json``.
    """

    bundle_version: str
    battle_id: str
    battle_repo: str
    battle_commit: str
    bench_kit_version: str
    ran_at: str
    schema_version: str

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


def package_bundle(result_path: Path, output_path: Path) -> Path:
    """Create ``output_path`` from ``result_path``; return ``output_path``.

    Validates the input result against ``result.schema.json`` and
    rejects bundles whose ``battle_commit`` is the synthetic nil
    SHA — those cannot be reproduced by any verifier and we'd rather
    fail loudly here than ship an unverifiable Claim.
    """
    if not result_path.is_file():
        msg = f"result file does not exist: {result_path}"
        raise PackageError(msg)

    try:
        result = json.loads(result_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        msg = f"result file is not valid JSON: {exc}"
        raise PackageError(msg) from exc

    try:
        Draft202012Validator(load_schema("result")).validate(result)
    except ValidationError as exc:
        loc = "/".join(str(p) for p in exc.absolute_path) or "<root>"
        msg = f"result.json does not conform to the schema at {loc}: {exc.message}"
        raise PackageError(msg) from exc

    if result["battle_commit"] == _NIL_COMMIT:
        msg = (
            "result.json has a nil battle_commit; the run was not in a git "
            "context and cannot be reproduced by a verifier. Re-run from a "
            "clean checkout of `krabarena-bench` and try again."
        )
        raise PackageError(msg)

    runs_root = result_path.parent / "runs"

    meta = BundleMeta(
        bundle_version=_BUNDLE_VERSION,
        battle_id=result["battle_id"],
        battle_repo=result["battle_repo"],
        battle_commit=result["battle_commit"],
        bench_kit_version=_BENCH_KIT_VERSION,
        ran_at=result["ran_at"],
        schema_version=result["schema_version"],
    )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    # gzip header normally records the source filename and current mtime;
    # both make the bundle non-deterministic across hosts and runs.
    # Manage the gzip layer ourselves so identical inputs produce identical
    # bytes, and verifiers can hash bundles meaningfully.
    with (
        output_path.open("wb") as raw,
        gzip.GzipFile(filename="", fileobj=raw, mtime=0, mode="wb") as gz,
        tarfile.open(fileobj=gz, mode="w") as tar,
    ):
        _add_bytes(tar, "meta.json", json.dumps(meta.to_dict(), indent=2).encode("utf-8"))
        _add_bytes(tar, "result.json", result_path.read_bytes())
        if runs_root.is_dir():
            _add_runs_tree(tar, runs_root)
    return output_path


def _add_bytes(tar: tarfile.TarFile, arcname: str, data: bytes) -> None:
    info = tarfile.TarInfo(name=arcname)
    info.size = len(data)
    info.mtime = 0  # deterministic — same input bytes produce the same tar
    tar.addfile(info, io.BytesIO(data))


def _add_runs_tree(tar: tarfile.TarFile, runs_root: Path) -> None:
    """Add ``runs_root`` recursively under ``runs/`` in the tar.

    We rebuild TarInfo manually instead of ``tar.add(..., recursive=True)``
    so the bundle is byte-deterministic — identical inputs produce
    identical bytes regardless of host mtime / uid / gid. Verifiers
    can rely on the bundle hash.
    """
    files = sorted(p for p in runs_root.rglob("*") if p.is_file())
    for path in files:
        rel = path.relative_to(runs_root.parent).as_posix()
        data = path.read_bytes()
        info = tarfile.TarInfo(name=rel)
        info.size = len(data)
        info.mtime = 0
        info.uid = 0
        info.gid = 0
        info.uname = ""
        info.gname = ""
        info.mode = 0o644
        tar.addfile(info, io.BytesIO(data))


def read_bundle_json(bundle_path: Path, member: str) -> dict[str, Any]:
    """Extract ``member`` out of ``bundle_path`` and parse it as JSON.

    Used by both :func:`read_bundle_meta` and :mod:`bench_kit.verify` —
    the latter pulls ``result.json`` from the same bundle. Centralising
    keeps the gzip+tar+JSON-decode boilerplate (and its error paths) in
    one place.
    """
    if not bundle_path.is_file():
        msg = f"bundle does not exist: {bundle_path}"
        raise PackageError(msg)
    with gzip.open(bundle_path, "rb") as gz, tarfile.open(fileobj=gz, mode="r") as tar:
        try:
            entry = tar.getmember(member)
        except KeyError as exc:
            msg = f"bundle missing {member}: {bundle_path}"
            raise PackageError(msg) from exc
        f = tar.extractfile(entry)
        if f is None:
            msg = f"bundle {member} could not be opened"
            raise PackageError(msg)
        out: dict[str, Any] = json.loads(f.read().decode("utf-8"))
        return out


def read_bundle_meta(bundle_path: Path) -> BundleMeta:
    """Read ``meta.json`` out of an existing bundle without unpacking."""
    data = read_bundle_json(bundle_path, "meta.json")
    required = (
        "bundle_version",
        "battle_id",
        "battle_repo",
        "battle_commit",
        "bench_kit_version",
        "ran_at",
        "schema_version",
    )
    missing = [k for k in required if k not in data]
    if missing:
        msg = f"bundle meta.json is missing fields: {sorted(missing)}"
        raise PackageError(msg)
    return BundleMeta(
        bundle_version=str(data["bundle_version"]),
        battle_id=str(data["battle_id"]),
        battle_repo=str(data["battle_repo"]),
        battle_commit=str(data["battle_commit"]),
        bench_kit_version=str(data["bench_kit_version"]),
        ran_at=str(data["ran_at"]),
        schema_version=str(data["schema_version"]),
    )


__all__ = [
    "BundleMeta",
    "PackageError",
    "package_bundle",
    "read_bundle_json",
    "read_bundle_meta",
]
