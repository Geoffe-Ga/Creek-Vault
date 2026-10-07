"""The content-free benchmark report: summaries, verdicts, capacity basis.

:class:`BenchReport` is the only thing the harness serializes, and
:func:`write_report` is the only serializer. Every model in it is frozen,
refuses unknown fields, and holds only enums, numbers and pattern-constrained
identifiers (``tests/bench/test_report_schema_invariant.py`` walks the JSON
schema to keep it that way).

**Capacity basis for the admission caps.** ``/v1`` admits up to
:data:`~creek_mcp.httpapi.middleware.limits.DEFAULT_MAX_CONCURRENCY` requests
process-wide and
:data:`~creek_mcp.httpapi.middleware.limits.DEFAULT_MAX_PER_CONSUMER` per
consumer. Those are request-admission numbers with no model-capacity basis.
:class:`CapacityBasis` supplies one: the largest concurrency level at which
every level up to it fits the deadline, compared against both caps as read
from ``limits`` at report time. It reports; it never changes a cap.
"""

from collections.abc import Sequence
from pathlib import Path
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, PositiveFloat, PositiveInt

from creek_mcp.bench.deadline import Verdict, deadline_budget_seconds, deadline_verdict
from creek_mcp.bench.metadata import HostMetadata, RunMetadata
from creek_mcp.bench.stats import LatencySummary
from creek_mcp.bench.trial import Sweep, Trial
from creek_mcp.httpapi.middleware import limits

REPORT_SCHEMA_VERSION: Final[int] = 1
"""Bumped whenever a field changes meaning or is removed."""


class SweepSummary(BaseModel):
    """One sweep's trials, their aggregates, and its verdict."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    latency: LatencySummary
    verdict: Verdict
    trials: tuple[Trial, ...]


class CapacityBasis(BaseModel):
    """What the concurrency sweep says about the admission caps.

    Attributes:
        max_fitting_concurrency: Largest measured level at which it and every
            smaller measured level fit; ``None`` when even the smallest did
            not, or nothing was measured.
        admission_max_concurrency: The process-wide cap in force.
        admission_max_per_consumer: The per-consumer cap in force.
        process_cap_within_capacity: Whether the process cap is at or below
            the fitting level; ``None`` without a concurrency sweep.
        per_consumer_cap_within_capacity: The same for the per-consumer cap.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    max_fitting_concurrency: PositiveInt | None
    admission_max_concurrency: PositiveInt
    admission_max_per_consumer: PositiveInt
    process_cap_within_capacity: bool | None
    per_consumer_cap_within_capacity: bool | None


class BenchReport(BaseModel):
    """The whole benchmark result, safe to share as evidence.

    Attributes:
        schema_version: :data:`REPORT_SCHEMA_VERSION`.
        kind: Always ``benchmark`` — measured, unlike a cost ``model``.
        run: Pinned run metadata.
        host: Capacity-only host metadata.
        budget_seconds: The deadline the verdicts were judged against.
        per_sweep: Each sweep that ran.
        verdict: ``exceeds`` if any sweep exceeds; else ``insufficient_data``
            if any sweep lacked a success; else ``fits``.
        capacity: The concurrency sweep's basis for the admission caps.
        quality_score: Reserved for the B22 quality evaluation; always
            ``None`` here.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal[1] = 1
    kind: Literal["benchmark"] = "benchmark"
    run: RunMetadata
    host: HostMetadata
    budget_seconds: PositiveFloat
    per_sweep: dict[Sweep, SweepSummary]
    verdict: Verdict
    capacity: CapacityBasis
    quality_score: None = None


def overall_verdict(verdicts: Sequence[Verdict]) -> Verdict:
    """Combine per-sweep verdicts; any failure dominates, then any gap."""
    if Verdict.EXCEEDS in verdicts:
        return Verdict.EXCEEDS
    if not verdicts or Verdict.INSUFFICIENT_DATA in verdicts:
        return Verdict.INSUFFICIENT_DATA
    return Verdict.FITS


def _max_fitting_level(trials: Sequence[Trial], budget: float) -> int | None:
    """Return the largest level such that it and every smaller level fit."""
    fitting: int | None = None
    for level in sorted({trial.concurrency for trial in trials}):
        group = [trial for trial in trials if trial.concurrency == level]
        if (
            deadline_verdict(LatencySummary.from_trials(group), budget)
            is not Verdict.FITS
        ):
            break
        fitting = level
    return fitting


def capacity_basis(trials: Sequence[Trial], budget: float) -> CapacityBasis:
    """Judge the admission caps against the concurrency sweep's *trials*."""
    process_cap = limits.DEFAULT_MAX_CONCURRENCY
    consumer_cap = limits.DEFAULT_MAX_PER_CONSUMER
    if not trials:
        return CapacityBasis(
            max_fitting_concurrency=None,
            admission_max_concurrency=process_cap,
            admission_max_per_consumer=consumer_cap,
            process_cap_within_capacity=None,
            per_consumer_cap_within_capacity=None,
        )
    fitting = _max_fitting_level(trials, budget)
    return CapacityBasis(
        max_fitting_concurrency=fitting,
        admission_max_concurrency=process_cap,
        admission_max_per_consumer=consumer_cap,
        process_cap_within_capacity=fitting is not None and process_cap <= fitting,
        per_consumer_cap_within_capacity=fitting is not None
        and consumer_cap <= fitting,
    )


def build_report(
    run: RunMetadata, host: HostMetadata, trials: Sequence[Trial]
) -> BenchReport:
    """Group *trials* by sweep, judge each, and assemble the report.

    The budget is read once, so every verdict in one report is judged against
    the same number the report records.
    """
    budget = deadline_budget_seconds()
    per_sweep: dict[Sweep, SweepSummary] = {}
    for sweep in Sweep:
        group = tuple(trial for trial in trials if trial.sweep is sweep)
        if not group:
            continue
        latency = LatencySummary.from_trials(group)
        per_sweep[sweep] = SweepSummary(
            latency=latency,
            verdict=deadline_verdict(latency, budget),
            trials=group,
        )
    concurrency = [trial for trial in trials if trial.sweep is Sweep.CONCURRENCY]
    return BenchReport(
        run=run,
        host=host,
        budget_seconds=budget,
        per_sweep=per_sweep,
        verdict=overall_verdict([summary.verdict for summary in per_sweep.values()]),
        capacity=capacity_basis(concurrency, budget),
    )


def write_report(report: BenchReport, path: Path) -> None:
    """Serialize *report* as JSON to *path* — the harness's only serializer."""
    path.write_text(report.model_dump_json(indent=2) + "\n", encoding="utf-8")


def load_report(path: Path) -> BenchReport:
    """Read a report written by :func:`write_report`, validating it fully."""
    return BenchReport.model_validate_json(path.read_text(encoding="utf-8"))
