"""JSON Schemas for the krabarena-bench format.

Schemas are loaded by path so they remain authoritative-as-data and
can be served as-is to other implementations.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

_SCHEMA_DIR = Path(__file__).parent


def load_schema(name: str) -> dict[str, Any]:
    """Return a JSON Schema by short name (e.g. ``"result"``, ``"meta"``)."""
    path = _SCHEMA_DIR / f"{name}.schema.json"
    with path.open(encoding="utf-8") as f:
        data: dict[str, Any] = json.load(f)
    return data


def schema_path(name: str) -> Path:
    """Return the absolute path to a schema file."""
    return _SCHEMA_DIR / f"{name}.schema.json"


__all__ = ["load_schema", "schema_path"]
