"""krabarena-bench reference implementation.

See ``SPEC.md`` at the repository root for the full contract.
"""

from __future__ import annotations

from importlib import metadata

try:
    __version__ = metadata.version("krabarena-bench-kit")
except metadata.PackageNotFoundError:  # editable install before install
    __version__ = "0.1.0.dev0"

SPEC_VERSION = "0.1.0"

__all__ = ["SPEC_VERSION", "__version__"]
