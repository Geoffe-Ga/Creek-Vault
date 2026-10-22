"""Nearest-rank percentiles and error rates over benchmark trials.

Percentiles are computed over **successful trials only**, and the error rate
over all of them. Mixing the two would let a fast failure (an Ollama
out-of-memory 500 returns in milliseconds) pull p95 *down*, making a failing
envelope look faster. The deadline verdict closes the other half of that gap
by treating any failure as not fitting.
"""

import math
from collections.abc import Sequence
from typing import Final, Self

from pydantic import BaseModel, ConfigDict, NonNegativeFloat, NonNegativeInt

from creek_mcp.bench.outcome import Outcome
from creek_mcp.bench.trial import Trial
from creek_mcp.bench.usage import TokenBasis

P50: Final[float] = 0.50
"""The median."""

P95: Final[float] = 0.95
"""The tail percentile the deadline verdict is judged on."""

_RANK_DIGITS: Final[int] = 9
"""Decimal places ``q * n`` is rounded to before ``ceil``.

``0.07 * 100`` evaluates to ``7.000000000000001``; without rounding, ``ceil``
would promote that to rank 8. Nine places is far finer than any real ``q``.
"""

_EMPTY_SAMPLES: Final[str] = "no samples to take a percentile of"
_BAD_QUANTILE: Final[str] = "quantile must satisfy 0 < q <= 1"
_EMPTY_TRIALS: Final[str] = "no trials to take an error rate of"


def percentile_nearest_rank(values: Sequence[float], q: float) -> float:
    """Return the nearest-rank *q* percentile of *values*.

    The nearest-rank definition always returns an observed sample: the value
    at 1-based rank ``ceil(q * n)`` of the sorted samples.

    Args:
        values: The samples; order does not matter.
        q: The quantile, ``0 < q <= 1``.

    Returns:
        The observed sample at that rank.

    Raises:
        ValueError: When *values* is empty or *q* is out of range.
    """
    if not values:
        raise ValueError(_EMPTY_SAMPLES)
    if not 0 < q <= 1:
        raise ValueError(_BAD_QUANTILE)
    ordered = sorted(values)
    rank = math.ceil(round(q * len(ordered), _RANK_DIGITS))
    return ordered[rank - 1]


def error_rate(trials: Sequence[Trial]) -> float:
    """Return the fraction of *trials* whose outcome is not ``ok``.

    Raises:
        ValueError: When *trials* is empty.
    """
    if not trials:
        raise ValueError(_EMPTY_TRIALS)
    failed = sum(1 for trial in trials if trial.outcome is not Outcome.OK)
    return failed / len(trials)


def _optional_percentile(values: Sequence[float], q: float) -> float | None:
    """Return the percentile, or ``None`` when there are no samples."""
    return percentile_nearest_rank(values, q) if values else None


class LatencySummary(BaseModel):
    """Aggregates over one group of trials.

    Attributes:
        count: Trials in the group.
        ok_count: Trials that ended ``ok``.
        error_rate: Fraction of trials that did not end ``ok``.
        p50_s: Median latency of ``ok`` trials, ``None`` when there were none.
        p95_s: p95 latency of ``ok`` trials, ``None`` when there were none.
        generation_p95_s: p95 of the model-call share of ``ok`` trials.
        ttft_p95_s: p95 time to first token over the ``ok`` trials whose
            backend reported one, ``None`` when none did. A non-streaming
            backend therefore leaves this unset rather than contributing a
            zero, which would claim an instant first token, or the whole
            latency, which would claim a slow one. ADR-0016 R2 budgets it at
            ≤ 2 s.
        outcome_counts: Trials per outcome, every outcome present.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    count: NonNegativeInt
    ok_count: NonNegativeInt
    error_rate: NonNegativeFloat
    p50_s: NonNegativeFloat | None
    p95_s: NonNegativeFloat | None
    generation_p95_s: NonNegativeFloat | None
    ttft_p95_s: NonNegativeFloat | None
    outcome_counts: dict[Outcome, NonNegativeInt]

    @classmethod
    def from_trials(cls, trials: Sequence[Trial]) -> Self:
        """Summarise *trials*; percentiles use ``ok`` trials only.

        Raises:
            ValueError: When *trials* is empty.
        """
        ok = [trial for trial in trials if trial.outcome is Outcome.OK]
        latencies = [trial.latency_s for trial in ok]
        generations = [t.generation_s for t in ok if t.generation_s is not None]
        ttfts = [t.ttft_s for t in ok if t.ttft_s is not None]
        counts = dict.fromkeys(Outcome, 0)
        for trial in trials:
            counts[trial.outcome] += 1
        return cls(
            count=len(trials),
            ok_count=len(ok),
            error_rate=error_rate(trials),
            p50_s=_optional_percentile(latencies, P50),
            p95_s=_optional_percentile(latencies, P95),
            generation_p95_s=_optional_percentile(generations, P95),
            ttft_p95_s=_optional_percentile(ttfts, P95),
            outcome_counts=counts,
        )


class TokenSummary(BaseModel):
    """Aggregates over one group of measured token counts.

    The sibling of :class:`LatencySummary` for the counts that replace
    ADR-0016's planning assumption (#1893). Percentiles use the same
    nearest-rank definition, so every reported figure is an observed sample.

    Attributes:
        count: Samples in the group — trials whose provider reported a count.
        p50: Median, or ``None`` when nothing was reported.
        p95: The tail a capacity figure is sized against, or ``None``.
        mean: Arithmetic mean, or ``None``. Carried beside the percentiles
            because a per-1,000-reflections cost is a sum, and a sum is the
            mean times the count — not the median times the count.
        max: Largest sample, or ``None``.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    count: NonNegativeInt
    p50: NonNegativeInt | None
    p95: NonNegativeInt | None
    mean: NonNegativeFloat | None
    max: NonNegativeInt | None

    @classmethod
    def from_counts(cls, counts: Sequence[int]) -> Self:
        """Summarise *counts*; an empty group measures nothing, not zero."""
        if not counts:
            return cls(count=0, p50=None, p95=None, mean=None, max=None)
        return cls(
            count=len(counts),
            p50=int(percentile_nearest_rank(counts, P50)),
            p95=int(percentile_nearest_rank(counts, P95)),
            mean=sum(counts) / len(counts),
            max=max(counts),
        )


class TokenUsageSummary(BaseModel):
    """Measured prompt and output tokens over one group of trials.

    Only ``ok`` trials contribute, for the reason :class:`LatencySummary`
    gives: a trial that failed fast measured nothing, and letting it in would
    drag a percentile toward a number no reflection ever cost.

    Attributes:
        prompt: Prompt-token aggregates over the reporting ``ok`` trials.
        output: Output-token aggregates over the same trials.
        reported_count: ``ok`` trials whose provider reported *either* count.
            Keyed on either, not on the prompt count alone, because a runtime
            may send one counter and omit the other — and then the trial does
            feed one of the summaries above, so calling it unreported would
            make ``output.count`` exceed ``reported_count``.
        unreported_count: ``ok`` trials whose provider reported neither count.
            Non-zero here means the figures above describe a subset, so a cost
            line must say so.
        output_cap_exceeded_count: ``ok`` trials whose measured output ran past
            the ceiling ``reflect_tool`` asked for. Reported, never clipped.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    prompt: TokenSummary
    output: TokenSummary
    reported_count: NonNegativeInt
    unreported_count: NonNegativeInt
    output_cap_exceeded_count: NonNegativeInt

    @classmethod
    def from_trials(cls, trials: Sequence[Trial]) -> Self:
        """Summarise the measured tokens of the ``ok`` trials in *trials*."""
        ok = [trial for trial in trials if trial.outcome is Outcome.OK]
        prompts = [t.prompt_tokens for t in ok if t.prompt_tokens is not None]
        outputs = [t.output_tokens for t in ok if t.output_tokens is not None]
        reported = [t for t in ok if _reported_any(t)]
        return cls(
            prompt=TokenSummary.from_counts(prompts),
            output=TokenSummary.from_counts(outputs),
            reported_count=len(reported),
            unreported_count=len(ok) - len(reported),
            output_cap_exceeded_count=sum(1 for t in ok if _overran_budget(t)),
        )


def _reported_any(trial: Trial) -> bool:
    """Return whether *trial*'s provider reported either token count."""
    return trial.prompt_tokens is not None or trial.output_tokens is not None


def _overran_budget(trial: Trial) -> bool:
    """Return whether *trial*'s measured output passed the ceiling asked for."""
    if trial.output_tokens is None or trial.max_output_tokens is None:
        return False
    return trial.output_tokens > trial.max_output_tokens


def _rate(trial: Trial) -> float | None:
    """Return *trial*'s output tokens per second, or ``None`` if it has none.

    A rate needs both a count and a positive model-call time. A zero-second
    generation is not an infinite rate, it is an unmeasurable one.
    """
    if trial.output_tokens is None or trial.generation_s is None:
        return None
    if trial.generation_s <= 0:
        return None
    return trial.output_tokens / trial.generation_s


def _throughput_basis(rated: Sequence[Trial], citable: Sequence[Trial]) -> TokenBasis:
    """Return the basis the reported throughput rests on.

    A citable sample makes the figure provider-reported. With none, the basis
    says *why* there is no figure: a byte upper bound is a bound that was
    refused, while an absent label is a backend that counted nothing.
    """
    if citable:
        return TokenBasis.PROVIDER_REPORTED
    bases = {t.token_basis for t in rated if t.token_basis is not None}
    if TokenBasis.BYTE_UPPER_BOUND in bases:
        return TokenBasis.BYTE_UPPER_BOUND
    return TokenBasis.UNREPORTED


class ThroughputSummary(BaseModel):
    """Output tokens per second over one group of trials.

    **Only a provider-reported count may state a rate.** A
    :attr:`~creek_mcp.bench.usage.TokenBasis.BYTE_UPPER_BOUND` count bounds
    *bytes*, so dividing it by a measured generation time produces an upper
    bound on throughput dressed as a measurement — and an upper bound on
    throughput flatters the backend, which is the direction a capacity figure
    must never err in. Such samples are counted in ``excluded_count`` and the
    figures stay ``None``, which :mod:`creek_mcp.provisioning.pool_bundle`'s
    ``size_pool_caps`` refuses on the same grounds.

    Only ``ok`` trials contribute, for :class:`LatencySummary`'s reason: a
    trial that failed fast generated nothing.

    Attributes:
        basis: What the figures rest on, or why there are none.
        count: Citable samples the figures were taken over.
        excluded_count: ``ok`` trials that could have stated a rate but whose
            counts were not citable. Non-zero with a ``None`` figure means the
            run measured throughput it may not quote.
        tokens_per_second_p50: Median rate, or ``None``.
        tokens_per_second_p95: The rate at the p95 rank. **This is the
            favourable tail**, not the worst case: a high value is the run's
            fastest generation, so a latency budget is judged on
            :attr:`LatencySummary.p95_s`, never on this.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    basis: TokenBasis
    count: NonNegativeInt
    excluded_count: NonNegativeInt
    tokens_per_second_p50: NonNegativeFloat | None
    tokens_per_second_p95: NonNegativeFloat | None

    @classmethod
    def from_trials(cls, trials: Sequence[Trial]) -> Self:
        """Summarise the citable throughput of the ``ok`` trials in *trials*."""
        ok = [t for t in trials if t.outcome is Outcome.OK]
        rated = [t for t in ok if _rate(t) is not None]
        citable = [t for t in rated if t.token_basis is TokenBasis.PROVIDER_REPORTED]
        rates = [rate for t in citable if (rate := _rate(t)) is not None]
        return cls(
            basis=_throughput_basis(rated, citable),
            count=len(citable),
            excluded_count=len(rated) - len(citable),
            tokens_per_second_p50=_optional_percentile(rates, P50),
            tokens_per_second_p95=_optional_percentile(rates, P95),
        )
