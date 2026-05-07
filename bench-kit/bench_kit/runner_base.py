"""Public Runner ABI — see SPEC.md §5.

Every Battle's ``runners/<tool>.py`` defines exactly one subclass of
:class:`Runner`. The framework discovers it by looking for the single
:class:`Runner` subclass at module top level after importing the file
in a sandboxed loader.

This module is part of the stable public API; changes here are
breaking changes for every existing Battle.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar


@dataclass(frozen=True, slots=True)
class TaskLimits:
    """Resource limits for one iteration of a task.

    These are declared in ``tasks/<id>.yaml`` under ``limits:`` and
    apply identically to every tool — there are no per-tool overrides.
    """

    memory_mb: int
    cpus: float


@dataclass(frozen=True, slots=True)
class Task:
    """A single scenario every tool must run.

    Loaded from ``tasks/<id>.yaml``; passed to :meth:`Runner.run`. All
    fields are immutable for the lifetime of one bench run.
    """

    id: str
    title: str
    description: str
    fixture_service: str
    inputs: dict[str, Any]
    expected: dict[str, Any]
    success_predicate: str
    timeout_s: int
    iterations: int
    limits: TaskLimits

    # Resolved at run time by the framework, not by the YAML file.
    dir: Path = field(default=Path())
    results_dir: Path = field(default=Path())
    fixture_network: str = field(default="")


@dataclass(frozen=True, slots=True)
class RunMetrics:
    """Required metrics every runner must populate.

    Per SPEC.md §3.3, ``wall_clock_ms``, ``peak_rss_mb`` and
    ``cpu_time_ms`` are mandatory. Anything else is per-Battle and
    must be declared in ``meta.yaml`` under ``metrics.optional``.
    """

    wall_clock_ms: int
    peak_rss_mb: int
    cpu_time_ms: int
    extra: dict[str, int | float] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class RunResult:
    """The outcome of one iteration of one (tool, task) pair."""

    success: bool
    metrics: RunMetrics
    logs_path: Path
    """Absolute path to the **directory** that holds this iteration's logs.

    The orchestrator translates it to the schema-required relative
    form (``runs/<tool>/<task>/<iteration>``) when assembling
    ``result.json``. Runners using :func:`bench_kit.exec.run_task_in_sandbox`
    get the canonical layout for free; bespoke runners should write
    files into ``task.results_dir`` and return that path.
    """


class Runner(ABC):
    """Adapter that drives one Tool against any Task.

    Subclasses must declare two class attributes — :attr:`name` and
    :attr:`image` — and implement :meth:`run`. They must use only
    helpers from ``bench_kit`` for any external interaction; direct
    use of ``subprocess``, ``socket``, the ``docker`` SDK or HTTP
    libraries is rejected by ``bench validate``.
    """

    name: ClassVar[str]
    """Slug identifying the tool, e.g. ``"browserless"``."""

    image: ClassVar[str]
    """Upstream image reference. **Must** be sha256-pinned (no tags)."""

    @abstractmethod
    def run(self, task: Task) -> RunResult:
        """Run one iteration of ``task`` against this tool.

        The framework calls this exactly ``task.iterations`` times per
        bench run; it does not retry on failure. A failed iteration
        is recorded as ``success=False`` and contributes to the
        per-task ``success_rate`` summary metric.
        """


__all__ = [
    "RunMetrics",
    "RunResult",
    "Runner",
    "Task",
    "TaskLimits",
]
