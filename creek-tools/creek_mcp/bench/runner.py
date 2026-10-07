"""Run the capacity sweeps through the real ``creek.reflect`` path.

:func:`run_trial` is the only place the harness calls
:func:`creek_mcp.tools.reflect.reflect_tool`. It passes the production care
guard and an ``open`` ceiling over a synthetic corpus, so what is timed is a
real reflection — corpus walk, grounding, prompt build, model call, parse —
and what comes back is reduced to a content-free :class:`Trial` on the spot.
The reflection response itself is dropped.

The sweeps answer the four capacity questions in the D04 decision record:

- **cold/warm** — each cold trial first evicts the model and starts a fresh
  :class:`~creek_mcp.tools.reflect.GroundingSession` (a cold process); warm
  trials share one session with the model resident.
- **context** — one reflection per requested input size, refused up front if
  any size exceeds the pinned ``num_ctx``.
- **concurrency** — ``level`` reflections submitted together on ``level``
  threads, the way ``/v1`` serves reads in worker threads.
- **idle** — evict, wait the idle period, then resume.

Every refusal — a malformed workload, a cloud provider, an unpinned live run —
happens before the corpus is written or a request is sent.
"""

from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final

import httpx

from creek.care.guardrail import acute_distress_guard
from creek_mcp.bench.corpus import Corpus, build_corpus, entry_text
from creek_mcp.bench.metadata import RunMetadata, capture_host
from creek_mcp.bench.outcome import Outcome, TrialProbe, classify_outcome, probe_factory
from creek_mcp.bench.report import build_report
from creek_mcp.bench.trial import Phase, Sweep, Trial
from creek_mcp.tier_ceiling import TierCeiling
from creek_mcp.tools.reflect import GroundingSession, reflect_tool

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from creek.classify.privacy_filter import PrivacyTierOverride
    from creek_mcp.bench.local_only import LocalOnlyFactory
    from creek_mcp.bench.metadata import CpuKind
    from creek_mcp.bench.ollama_client import PinnedModel
    from creek_mcp.bench.protocols import Retriever
    from creek_mcp.bench.report import BenchReport

logger = logging.getLogger(__name__)

CONSUMER: Final[str] = "capacity-bench"
"""Consumer id on the synthetic corpus's audit rows."""

_ANSWERED: Final[frozenset[str]] = frozenset({"ok", "empty"})
"""Reflect statuses meaning the model answered.

``empty`` (no verbatim-valid note survived parsing) is a *quality* result, not
a capacity failure: the model was reached and replied within the deadline.
Quality is scored separately (the report's ``quality_score`` hook).
"""

_TRIAL_FAILURES: Final[tuple[type[BaseException], ...]] = (
    MemoryError,
    OSError,
    httpx.HTTPError,
    RuntimeError,
    ValueError,
)
"""Exception types a trial may end in. Anything else is a harness bug."""

_BAD_LEVEL: Final[str] = "concurrency levels must be at least 1"
_BAD_COUNT: Final[str] = "trial counts must be non-negative"
_BAD_IDLE: Final[str] = "idle_seconds must be non-negative"
_BAD_CONTEXT: Final[str] = (
    "context sizes must be between 1 and num_ctx minus the prompt overhead "
    "and num_predict"
)

PROMPT_OVERHEAD_TOKENS: Final[int] = 1536
"""Token allowance for reflect's prompt around the entry.

An upper bound, not an estimate: it covers the template's *byte* length with
every grounding slot filled and the largest note budget (1,448 bytes at
``a5d28a5``), and a byte-level tokenizer never spends less than one byte per
token. ``tests/bench/test_runner.py`` fails if reflect's template outgrows it.
"""
_LIVE_NEEDS_PIN: Final[str] = "a live run needs a digest-verifying backend"


class WorkloadError(ValueError):
    """The requested workload cannot be run as specified."""


@dataclass(frozen=True, slots=True)
class Workload:
    """What to measure.

    Attributes:
        seed: Corpus and entry-text seed.
        entries: Synthetic notes in the corpus.
        words_per_entry: Words per corpus note.
        query_words: Words in each cold/warm/concurrency/idle entry.
        cold_trials: Cold trials (model evicted, fresh grounding session).
        warm_trials: Warm trials sharing one session.
        context_sizes: Entry sizes (words) for the context sweep.
        concurrency_levels: Simultaneous reflections per concurrency step.
        idle_cycles: Evict-wait-resume cycles.
        idle_seconds: The wait in each idle cycle.
    """

    seed: int = 0
    entries: int = 8
    words_per_entry: int = 64
    query_words: int = 64
    cold_trials: int = 2
    warm_trials: int = 3
    context_sizes: tuple[int, ...] = ()
    concurrency_levels: tuple[int, ...] = (1,)
    idle_cycles: int = 0
    idle_seconds: float = 0.0


@dataclass(frozen=True, slots=True)
class Backend:
    """The model runtime the trials drive.

    Attributes:
        factory: The only factory type the runner accepts.
        evict: Unload the model so the next trial is cold.
        resident_bytes: The model's resident size, or ``None``.
        pin: Live only: verify the pinned digest and describe the weights.
    """

    factory: LocalOnlyFactory
    evict: Callable[[], None]
    resident_bytes: Callable[[], int | None]
    pin: Callable[[str], PinnedModel] | None = None


@dataclass(frozen=True, slots=True)
class BenchPlan:
    """A complete, validated-on-run benchmark specification.

    Attributes:
        metadata: The run's pinned metadata.
        workload: What to measure.
        backend: The model runtime.
        clock: Monotonic clock for latencies.
        sleeper: Waits out each idle period.
    """

    metadata: RunMetadata
    workload: Workload
    backend: Backend
    clock: Callable[[], float] = time.perf_counter
    sleeper: Callable[[float], None] = time.sleep


@dataclass(frozen=True, slots=True)
class _TrialSpec:
    """One trial to run."""

    sweep: Sweep
    phase: Phase
    concurrency: int
    content: str


@dataclass(slots=True)
class _Harness:
    """Per-run state shared by the sweeps."""

    plan: BenchPlan
    corpus: Corpus
    retrieve: Retriever | None
    next_index: int = field(default=0)

    def content(self, words: int) -> str:
        """Return fresh entry text of *words* words, never used before."""
        index = self.corpus.entry_count + self.next_index
        self.next_index += 1
        return entry_text(seed=self.plan.workload.seed, index=index, words=words)


def _no_grounding(query: str, vault: Path, override: PrivacyTierOverride) -> list[str]:
    """The hermetic grounder: nothing, so no embedding model is loaded."""
    del query, vault, override
    return []


def _trial_outcome(
    probe: TrialProbe, exc: BaseException | None, result: dict[str, Any] | None
) -> Outcome:
    """Decide a trial's outcome from what the probe and reflect each saw."""
    if probe.outcome is not None:
        return probe.outcome
    if exc is not None:
        return classify_outcome(exc)
    if result is not None and result.get("status") in _ANSWERED:
        return Outcome.OK
    return Outcome.ERROR


def run_trial(
    harness: _Harness, spec: _TrialSpec, session: GroundingSession | None
) -> Trial:
    """Run one reflection and reduce it to a content-free :class:`Trial`.

    Only the exception's *type name* is ever logged: a message can carry
    model output or entry text.
    """
    plan = harness.plan
    probe = TrialProbe(clock=plan.clock)
    exc: BaseException | None = None
    result: dict[str, Any] | None = None
    start = plan.clock()
    try:
        result = reflect_tool(
            vault_path=harness.corpus.root,
            llm_factory=probe_factory(plan.backend.factory, probe),
            content=spec.content,
            retrieve=harness.retrieve,
            care_guard=acute_distress_guard,
            session=session,
            privacy_tier_ceiling=TierCeiling.OPEN,
            consumer=CONSUMER,
        )
    except _TRIAL_FAILURES as caught:
        exc = caught
    latency = plan.clock() - start
    outcome = _trial_outcome(probe, exc, result)
    if exc is not None:
        logger.debug("bench trial raised %s", type(exc).__name__)
    logger.info(
        "bench trial sweep=%s phase=%s concurrency=%d outcome=%s latency_s=%.3f",
        spec.sweep.value,
        spec.phase.value,
        spec.concurrency,
        outcome.value,
        latency,
    )
    return Trial(
        sweep=spec.sweep,
        phase=spec.phase,
        concurrency=spec.concurrency,
        input_words=len(spec.content.split()),
        latency_s=max(latency, 0.0),
        generation_s=probe.generation_s,
        outcome=outcome,
        model_resident_bytes=_resident_bytes(plan),
    )


def _resident_bytes(plan: BenchPlan) -> int | None:
    """Best-effort residency probe: an unreachable runtime means unknown.

    Called after the trial is already decided, so a runtime that dropped
    mid-run must not turn a classified trial into a crashed run.
    """
    try:
        return plan.backend.resident_bytes()
    except _TRIAL_FAILURES as exc:
        logger.debug("bench residency probe raised %s", type(exc).__name__)
        return None


def _evict_then_run(
    harness: _Harness,
    spec: _TrialSpec,
    session: GroundingSession,
    *,
    pause_s: float | None = None,
) -> Trial:
    """Evict the model, then run *spec*; a failed evict is the trial's outcome.

    The eviction is part of what a cold or resumed trial measures, so when it
    fails (the runtime dropped, typically) the trial records that classified
    failure with no latency rather than aborting every remaining sweep.
    *pause_s*, when given, is waited out between the eviction and the trial
    (the idle cycle).
    """
    try:
        harness.plan.backend.evict()
    except _TRIAL_FAILURES as exc:
        logger.debug("bench evict raised %s", type(exc).__name__)
        return Trial(
            sweep=spec.sweep,
            phase=spec.phase,
            concurrency=spec.concurrency,
            input_words=len(spec.content.split()),
            latency_s=0.0,
            generation_s=None,
            outcome=classify_outcome(exc),
            model_resident_bytes=None,
        )
    if pause_s is not None:
        harness.plan.sleeper(pause_s)
    return run_trial(harness, spec, session)


def _cold_warm(harness: _Harness) -> list[Trial]:
    """Cold trials (evict + fresh session first), then warm ones."""
    workload = harness.plan.workload
    trials = []
    for _ in range(workload.cold_trials):
        spec = _TrialSpec(
            Sweep.COLD_WARM, Phase.COLD, 1, harness.content(workload.query_words)
        )
        trials.append(_evict_then_run(harness, spec, GroundingSession()))
    session = GroundingSession()
    for _ in range(workload.warm_trials):
        spec = _TrialSpec(
            Sweep.COLD_WARM, Phase.WARM, 1, harness.content(workload.query_words)
        )
        trials.append(run_trial(harness, spec, session))
    return trials


def _context(harness: _Harness, session: GroundingSession) -> list[Trial]:
    """One warm reflection per requested input size."""
    return [
        run_trial(
            harness,
            _TrialSpec(Sweep.CONTEXT, Phase.WARM, 1, harness.content(size)),
            session,
        )
        for size in harness.plan.workload.context_sizes
    ]


def _concurrency(harness: _Harness, session: GroundingSession) -> list[Trial]:
    """``level`` reflections in flight together, for each level."""
    workload = harness.plan.workload
    trials: list[Trial] = []
    for level in workload.concurrency_levels:
        specs = [
            _TrialSpec(
                Sweep.CONCURRENCY,
                Phase.WARM,
                level,
                harness.content(workload.query_words),
            )
            for _ in range(level)
        ]
        with ThreadPoolExecutor(max_workers=level) as pool:
            trials.extend(pool.map(lambda s: run_trial(harness, s, session), specs))
    return trials


def _idle(harness: _Harness, session: GroundingSession) -> list[Trial]:
    """Evict, wait the idle period, resume — once per cycle."""
    plan = harness.plan
    trials = []
    for _ in range(plan.workload.idle_cycles):
        spec = _TrialSpec(
            Sweep.IDLE, Phase.RESUME, 1, harness.content(plan.workload.query_words)
        )
        trials.append(
            _evict_then_run(harness, spec, session, pause_s=plan.workload.idle_seconds)
        )
    return trials


def max_context_words(metadata: RunMetadata) -> int:
    """Return the largest entry (in words) the context sweep may request.

    Every word is at least one token, so a size above this certainly
    overflows ``num_ctx`` once the template and the output are added. A size
    at or below it can still overflow when words tokenize to several tokens;
    the live client reports that as ``context_overflow`` rather than ``ok``.
    """
    return metadata.num_ctx - PROMPT_OVERHEAD_TOKENS - metadata.num_predict


def _validate(plan: BenchPlan) -> None:
    """Refuse a malformed or unpinnable plan before any I/O."""
    workload = plan.workload
    counts = (
        workload.cold_trials,
        workload.warm_trials,
        workload.idle_cycles,
        workload.entries,
        workload.words_per_entry,
        workload.query_words,
    )
    if any(level < 1 for level in workload.concurrency_levels):
        raise WorkloadError(_BAD_LEVEL)
    if any(count < 0 for count in counts):
        raise WorkloadError(_BAD_COUNT)
    if workload.idle_seconds < 0:
        raise WorkloadError(_BAD_IDLE)
    limit = max_context_words(plan.metadata)
    if any(not 0 < size <= limit for size in workload.context_sizes):
        raise WorkloadError(_BAD_CONTEXT)
    if plan.metadata.mode == "live" and plan.backend.pin is None:
        raise WorkloadError(_LIVE_NEEDS_PIN)


def _pinned_metadata(plan: BenchPlan) -> RunMetadata:
    """Verify a live run's digest and fold the weights' details into it."""
    metadata = plan.metadata
    if metadata.mode != "live" or plan.backend.pin is None or metadata.digest is None:
        return metadata
    pinned = plan.backend.pin(metadata.digest)
    return RunMetadata.model_validate(
        metadata.model_dump()
        | {
            "quantization": pinned.quantization,
            "parameter_count": pinned.parameter_count,
            "license_id": pinned.license_id,
        }
    )


_SAME_HOST_SCOPES: Final[frozenset[str]] = frozenset({"fake", "loopback"})
"""Endpoint scopes where the model runs on the harness's own host."""


def run_bench(
    plan: BenchPlan,
    corpus_root: Path,
    *,
    cpu_kind: CpuKind = "unknown",
    model_store: Path | None = None,
) -> BenchReport:
    """Run every sweep in *plan* and return the content-free report.

    Args:
        plan: What to run, against which backend.
        corpus_root: Absent or empty directory under the temp dir for the
            synthetic corpus; the caller owns its removal.
        cpu_kind: The Fly CPU class, recorded in the host metadata.
        model_store: The model store whose free disk to report; defaults to
            the corpus's temp directory (and is labelled as such).

    Returns:
        The benchmark report.

    Raises:
        WorkloadError: When the plan is malformed or a live run is unpinned.
        CloudProviderRefusedError: When the provider is not positively local.
        DigestMismatchError: When the served weights are not the pinned ones.
    """
    _validate(plan)
    plan.backend.factory.preflight()
    metadata = _pinned_metadata(plan)
    corpus = build_corpus(
        corpus_root,
        seed=plan.workload.seed,
        entries=plan.workload.entries,
        words_per_entry=plan.workload.words_per_entry,
    )
    retrieve = _no_grounding if metadata.grounding == "none" else None
    harness = _Harness(plan=plan, corpus=corpus, retrieve=retrieve)
    session = GroundingSession()
    trials = [
        *_cold_warm(harness),
        *_context(harness, session),
        *_concurrency(harness, session),
        *_idle(harness, session),
    ]
    host = capture_host(
        model_store or corpus.root,
        cpu_kind=cpu_kind,
        model_store=model_store is not None,
        describes_model_host=metadata.endpoint_scope in _SAME_HOST_SCOPES,
    )
    return build_report(metadata, host, trials)
