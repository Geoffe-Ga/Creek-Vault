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

from creek_mcp.bench.deadline import (
    BudgetTable,
    Verdict,
    budget_table,
    deadline_budget_seconds,
    deadline_verdict,
    overall_verdict,
)
from creek_mcp.bench.eval.rubric import QualityScore
from creek_mcp.bench.metadata import HostMetadata, RunMetadata
from creek_mcp.bench.stats import LatencySummary, ThroughputSummary, TokenUsageSummary
from creek_mcp.bench.tokens import TokensPerReflection
from creek_mcp.bench.trial import Phase, Sweep, Trial
from creek_mcp.httpapi.middleware import limits

REPORT_SCHEMA_VERSION: Final[int] = 1
"""Bumped whenever a field changes meaning or is removed.

Not bumped when ``quality_score`` started carrying a
:class:`~creek_mcp.bench.eval.rubric.QualityScore` (#1898). The field was
shipped documented as reserved for exactly that evaluation and was always
``null``, so filling it widens the type compatibly rather than changing what
the field means: a reader that handled ``null`` still reads every report this
version produces. What *would* be a bump is the rubric's own arithmetic
changing, and that has its own version —
:data:`creek_mcp.bench.eval.RUBRIC_VERSION`, recorded inside the score.

Not bumped for R2's budget table either (#1895). ``budgets``, ``throughput``,
``latency.ttft_p95_s`` and the trials' ``ttft_s`` / ``token_basis`` are all
*new* fields, and no field already in the schema changed meaning or was
removed: ``budget_seconds`` is still the deadline this process enforces and
``verdict`` is still the per-sweep verdict against it, with R2 answered beside
them in ``budgets.verdict`` rather than folded into either. A reader of this
version still parses every field it already knew, so the additive rule above
applies and :data:`creek_mcp.bench.HARNESS_VERSION` carries the change (1.2.0)
— which is the version a reader needing latency budgets or TTFT must require.
The three earlier changes #1915 records as having landed under version ``1``
are a different question — one of them, ``run.digest``, genuinely did change
meaning — and this note does not settle them.
"""


class PhaseSummary(BaseModel):
    """One residency phase's aggregates within a sweep, and its verdict.

    Attributes:
        latency: Latency aggregates over the phase's trials.
        tokens: Measured prompt and output tokens over the same trials, at the
            grain a cold start is judged on — a cold prompt is the same size as
            a warm one, so a difference here is the backend's, not the
            workload's.
        throughput: Output tokens per second over the same trials, reported
            only from provider-reported counts.
        verdict: The phase's deadline verdict.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    latency: LatencySummary
    tokens: TokenUsageSummary
    throughput: ThroughputSummary
    verdict: Verdict


class SweepSummary(BaseModel):
    """One sweep's trials, their aggregates, and its verdict.

    Attributes:
        latency: Aggregates over every trial in the sweep, for reference.
        tokens: Measured prompt and output tokens over every trial in the
            sweep. The context sweep's block is the one that shows how token
            cost grows with input size.
        per_phase: Aggregates and a verdict per residency phase. Cold and warm
            trials are judged apart: pooled, a few slow cold starts fall
            outside the p95 rank of many warm trials and would go unseen.
        throughput: Output tokens per second over every trial in the sweep,
            reported only from provider-reported counts. ``None`` figures with
            a ``byte_upper_bound`` basis are a fake run refusing to state a
            rate over bounded bytes, not a backend that generated nothing.
        verdict: The worse of the per-phase verdicts.
        trials: The sweep's trials.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    latency: LatencySummary
    tokens: TokenUsageSummary
    throughput: ThroughputSummary
    per_phase: dict[Phase, PhaseSummary]
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
        budget_seconds: The deadline the per-sweep verdicts were judged
            against — the one deadline *this* process enforces. ADR-0016 R2's
            four budgets are in ``budgets``, judged separately.
        per_sweep: Each sweep that ran.
        verdict: ``exceeds`` if any sweep exceeds; else ``insufficient_data``
            if any sweep lacked a success; else ``fits``. **This is the sweep
            verdict against ``budget_seconds`` and nothing more.** R2 is
            answered by ``budgets.verdict``, which is reported separately
            because a run can sit comfortably inside the shed deadline while
            measuring nothing R2 asks about.
        budgets: ADR-0016 R2's named budget table (#1895): one row per budget
            with its limit, the value judged against it, its own verdict, and
            whether production enforces it today. **No row in it is ever a
            production guarantee**, whatever the endpoint scope was: no run
            this harness can execute today dials an attesting pool, and the
            live pool run that would is #1899. ``model_off_harness_host``
            states only whether the model ran off this host.
        capacity: The concurrency sweep's basis for the admission caps.
        tokens_per_reflection: The measured per-surface token block (#1893).
            Every ADR-0016 cost figure cites this in place of the 2,500 + 128
            planning assumption; a figure quoting the assumption is an
            unmeasured placeholder.
        quality_score: The voice-fidelity eval's per-dimension scores and
            rubric version (#1898), or ``None`` for a capacity-only run. It is
            closed by construction like everything else here — enums, numbers
            and a semver — so filling it does not weaken the content-free
            invariant. The prompts and raw scores #1875 asks to be recorded
            live in the eval's own
            :class:`~creek_mcp.bench.eval.record.EvalRecord` artifact instead.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal[1] = 1
    kind: Literal["benchmark"] = "benchmark"
    run: RunMetadata
    host: HostMetadata
    budget_seconds: PositiveFloat
    per_sweep: dict[Sweep, SweepSummary]
    verdict: Verdict
    budgets: BudgetTable
    capacity: CapacityBasis
    tokens_per_reflection: TokensPerReflection
    quality_score: QualityScore | None = None


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


def _sweep_summary(group: tuple[Trial, ...], budget: float) -> SweepSummary:
    """Summarise one sweep, judging each residency phase on its own."""
    per_phase: dict[Phase, PhaseSummary] = {}
    for phase in Phase:
        trials = [trial for trial in group if trial.phase is phase]
        if trials:
            latency = LatencySummary.from_trials(trials)
            per_phase[phase] = PhaseSummary(
                latency=latency,
                tokens=TokenUsageSummary.from_trials(trials),
                throughput=ThroughputSummary.from_trials(trials),
                verdict=deadline_verdict(latency, budget),
            )
    return SweepSummary(
        latency=LatencySummary.from_trials(group),
        tokens=TokenUsageSummary.from_trials(group),
        throughput=ThroughputSummary.from_trials(group),
        per_phase=per_phase,
        verdict=overall_verdict([summary.verdict for summary in per_phase.values()]),
        trials=group,
    )


def build_report(
    run: RunMetadata,
    host: HostMetadata,
    trials: Sequence[Trial],
    tokens: TokensPerReflection,
    *,
    quality: QualityScore | None = None,
) -> BenchReport:
    """Group *trials* by sweep, judge each, and assemble the report.

    The budget is read once, so every verdict in one report is judged against
    the same number the report records.

    Args:
        run: Pinned run metadata.
        host: Capacity-only host metadata.
        trials: Every trial the sweeps produced.
        tokens: The per-surface measured token block, or
            :func:`~creek_mcp.bench.tokens.unmeasured_tokens` when the pass
            did not run.
        quality: The voice-fidelity eval's score (#1898), when one was run.
            ``None`` — the default — is a capacity-only report, which is what
            every existing caller produces.

    Returns:
        The assembled report.
    """
    budget = deadline_budget_seconds()
    per_sweep: dict[Sweep, SweepSummary] = {}
    for sweep in Sweep:
        group = tuple(trial for trial in trials if trial.sweep is sweep)
        if not group:
            continue
        per_sweep[sweep] = _sweep_summary(group, budget)
    concurrency = [trial for trial in trials if trial.sweep is Sweep.CONCURRENCY]
    return BenchReport(
        run=run,
        host=host,
        budget_seconds=budget,
        per_sweep=per_sweep,
        verdict=overall_verdict([summary.verdict for summary in per_sweep.values()]),
        budgets=budget_table(trials, describes_model_host=host.describes_model_host),
        capacity=capacity_basis(concurrency, budget),
        tokens_per_reflection=tokens,
        quality_score=quality,
    )


def write_report(report: BenchReport, path: Path) -> None:
    """Serialize *report* as JSON to *path* — the harness's only serializer."""
    path.write_text(report.model_dump_json(indent=2) + "\n", encoding="utf-8")


def load_report(path: Path) -> BenchReport:
    """Read a report written by :func:`write_report`, validating it fully."""
    return BenchReport.model_validate_json(path.read_text(encoding="utf-8"))
