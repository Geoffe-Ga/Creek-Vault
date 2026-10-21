"""Percentile and error-rate arithmetic for the capacity harness (#1850).

The percentiles feed the deadline-fit verdict, which is the evidence the D04
owner decisions consume, so every assertion here is an exact value chosen to
kill a specific off-by-one: nearest-rank must use ``ceil`` (not ``floor``,
``round`` or interpolation), and failures must count toward the error rate but
never toward the latency percentiles.
"""

from __future__ import annotations

import random

import pytest

from creek_mcp.bench.outcome import Outcome
from creek_mcp.bench.stats import (
    P50,
    P95,
    LatencySummary,
    ThroughputSummary,
    TokenSummary,
    TokenUsageSummary,
    error_rate,
    percentile_nearest_rank,
)
from creek_mcp.bench.trial import Phase, Sweep, Trial
from creek_mcp.bench.usage import TokenBasis


def _trial(
    latency_s: float,
    outcome: Outcome = Outcome.OK,
    *,
    prompt_tokens: int | None = None,
    output_tokens: int | None = None,
    max_output_tokens: int | None = None,
    generation_s: float | None = None,
    ttft_s: float | None = None,
    token_basis: TokenBasis | None = None,
) -> Trial:
    """Build one warm, serial trial with *latency_s* and *outcome*."""
    return Trial(
        sweep=Sweep.COLD_WARM,
        phase=Phase.WARM,
        concurrency=1,
        input_words=8,
        latency_s=latency_s,
        generation_s=generation_s,
        ttft_s=ttft_s,
        outcome=outcome,
        model_resident_bytes=None,
        prompt_tokens=prompt_tokens,
        output_tokens=output_tokens,
        max_output_tokens=max_output_tokens,
        token_basis=token_basis,
    )


def _rated(output_tokens: int, generation_s: float, basis: TokenBasis) -> Trial:
    """A successful trial whose counts carry *basis*."""
    return _trial(
        1.0,
        output_tokens=output_tokens,
        generation_s=generation_s,
        token_basis=basis,
    )


def test_ttft_p95_uses_only_the_trials_that_reported_one() -> None:
    """A non-streaming trial contributes no sample and no zero."""
    trials = [
        _trial(1.0, ttft_s=None),
        _trial(1.0, ttft_s=0.5),
        _trial(1.0, ttft_s=1.5),
    ]
    assert LatencySummary.from_trials(trials).ttft_p95_s == 1.5


def test_ttft_p95_is_none_when_nothing_streamed() -> None:
    """``None`` says unknown; a zero would claim an instant first token."""
    assert LatencySummary.from_trials([_trial(1.0)]).ttft_p95_s is None


def test_ttft_ignores_a_failed_trials_first_token() -> None:
    """A trial that failed is not a reflection anyone received."""
    trials = [_trial(1.0, Outcome.TIMEOUT, ttft_s=0.01), _trial(1.0, ttft_s=2.0)]
    assert LatencySummary.from_trials(trials).ttft_p95_s == 2.0


def test_throughput_is_measured_only_from_provider_reported_counts() -> None:
    """tokens/s = output tokens over the model call that produced them."""
    trials = [
        _rated(100, 2.0, TokenBasis.PROVIDER_REPORTED),
        _rated(300, 2.0, TokenBasis.PROVIDER_REPORTED),
    ]
    summary = ThroughputSummary.from_trials(trials)
    assert summary.basis is TokenBasis.PROVIDER_REPORTED
    assert summary.count == 2
    assert summary.excluded_count == 0
    assert summary.tokens_per_second_p50 == 50.0
    assert summary.tokens_per_second_p95 == 150.0


def test_a_byte_upper_bound_yields_no_throughput_figure() -> None:
    """A rate over bounded bytes would flatter the backend: refuse it."""
    summary = ThroughputSummary.from_trials(
        [_rated(100, 2.0, TokenBasis.BYTE_UPPER_BOUND)]
    )
    assert summary.basis is TokenBasis.BYTE_UPPER_BOUND
    assert summary.count == 0
    assert summary.excluded_count == 1
    assert summary.tokens_per_second_p50 is None
    assert summary.tokens_per_second_p95 is None


def test_a_bounded_sample_never_dilutes_a_citable_one() -> None:
    """Only the provider-reported trials feed the figure that is reported."""
    summary = ThroughputSummary.from_trials(
        [
            _rated(100, 2.0, TokenBasis.PROVIDER_REPORTED),
            _rated(999_999, 0.001, TokenBasis.BYTE_UPPER_BOUND),
        ]
    )
    assert summary.basis is TokenBasis.PROVIDER_REPORTED
    assert summary.count == 1
    assert summary.excluded_count == 1
    assert summary.tokens_per_second_p95 == 50.0


def test_a_backend_that_reported_no_counts_is_unreported_not_bounded() -> None:
    """An unlabelled trial is a gap in the counters, not an upper bound."""
    summary = ThroughputSummary.from_trials([_trial(1.0, generation_s=2.0)])
    assert summary.basis is TokenBasis.UNREPORTED
    assert summary.tokens_per_second_p95 is None


def test_throughput_needs_a_positive_generation_time() -> None:
    """A zero-second model call cannot state a rate; division is refused."""
    summary = ThroughputSummary.from_trials(
        [_rated(100, 0.0, TokenBasis.PROVIDER_REPORTED)]
    )
    assert summary.count == 0
    assert summary.excluded_count == 0
    assert summary.tokens_per_second_p50 is None


def test_throughput_ignores_failed_trials() -> None:
    """A failure generated nothing measurable, however fast it failed."""
    failed = _trial(
        0.01,
        Outcome.OOM,
        output_tokens=10,
        generation_s=0.001,
        token_basis=TokenBasis.PROVIDER_REPORTED,
    )
    summary = ThroughputSummary.from_trials([failed])
    assert summary.count == 0
    assert summary.tokens_per_second_p95 is None


def test_p50_p95_nearest_rank_exact() -> None:
    """Shuffled 1..20 gives p50 == 10 and p95 == 19 by nearest rank."""
    values = [float(v) for v in range(1, 21)]
    random.Random(4).shuffle(values)
    assert percentile_nearest_rank(values, P50) == 10.0
    assert percentile_nearest_rank(values, P95) == 19.0


def test_p50_boundary_kills_off_by_one() -> None:
    """Fractional ranks round up: [1..4] p95 is 4, [1..5] p50 is 3."""
    assert percentile_nearest_rank([1.0, 2.0, 3.0, 4.0], P50) == 2.0
    assert percentile_nearest_rank([1.0, 2.0, 3.0, 4.0], P95) == 4.0
    assert percentile_nearest_rank([1.0, 2.0, 3.0, 4.0, 5.0], P50) == 3.0


def test_rank_is_not_inflated_by_float_error() -> None:
    """``0.07 * 100`` is ``7.000000000000001``; the rank must still be 7."""
    values = [float(v) for v in range(1, 101)]
    assert percentile_nearest_rank(values, 0.07) == 7.0


def test_single_value() -> None:
    """One sample is every percentile."""
    assert percentile_nearest_rank([2.5], P50) == 2.5
    assert percentile_nearest_rank([2.5], P95) == 2.5


def test_empty_raises() -> None:
    """No samples means no percentile, never a silent zero."""
    with pytest.raises(ValueError, match="no samples"):
        percentile_nearest_rank([], P50)


@pytest.mark.parametrize("q", [0.0, 1.01, -0.5])
def test_q_out_of_range_raises(q: float) -> None:
    """Only ``0 < q <= 1`` is a percentile."""
    with pytest.raises(ValueError, match="quantile"):
        percentile_nearest_rank([1.0], q)


def test_q_of_one_is_the_maximum() -> None:
    """``q == 1`` is admitted and names the largest sample."""
    assert percentile_nearest_rank([3.0, 1.0, 2.0], 1.0) == 3.0


def test_error_rate_exact() -> None:
    """Three failures in eight trials is exactly 0.375."""
    trials = [_trial(1.0)] * 5 + [_trial(0.1, Outcome.OOM)] * 3
    assert error_rate(trials) == 0.375


def test_error_rate_empty_raises() -> None:
    """No trials has no error rate."""
    with pytest.raises(ValueError, match="no trials"):
        error_rate([])


def test_summary_percentiles_ignore_failures_but_error_rate_counts_them() -> None:
    """A fast OOM must not pull p95 down, yet it must raise the error rate."""
    trials = [_trial(float(v)) for v in range(1, 5)] + [
        _trial(0.001, Outcome.OOM),
        _trial(0.002, Outcome.TIMEOUT),
    ]
    summary = LatencySummary.from_trials(trials)
    assert summary.count == 6
    assert summary.ok_count == 4
    assert summary.p50_s == 2.0
    assert summary.p95_s == 4.0
    assert summary.error_rate == pytest.approx(2 / 6)


def test_summary_generation_percentiles_use_ok_trials_with_a_timing() -> None:
    """Generation p95 is over ok trials that recorded a generation time."""
    trials = [
        _trial(2.0).model_copy(update={"generation_s": 1.0}),
        _trial(3.0).model_copy(update={"generation_s": 2.0}),
        _trial(4.0),
        _trial(0.1, Outcome.ERROR).model_copy(update={"generation_s": 9.0}),
    ]
    summary = LatencySummary.from_trials(trials)
    assert summary.generation_p95_s == 2.0


def test_summary_with_no_ok_trials_has_no_percentiles() -> None:
    """All-failed trials leave percentiles unset, never zero."""
    summary = LatencySummary.from_trials([_trial(0.1, Outcome.OOM)])
    assert summary.p50_s is None
    assert summary.p95_s is None
    assert summary.generation_p95_s is None
    assert summary.error_rate == 1.0


def test_outcome_counts_zero_filled() -> None:
    """Every outcome appears in the counts, zero when it never occurred."""
    summary = LatencySummary.from_trials([_trial(1.0), _trial(0.1, Outcome.OOM)])
    assert summary.outcome_counts == {
        Outcome.OK: 1,
        Outcome.TIMEOUT: 0,
        Outcome.OOM: 1,
        Outcome.DISK_FULL: 0,
        Outcome.PROVIDER_UNAVAILABLE: 0,
        Outcome.CONTEXT_OVERFLOW: 0,
        Outcome.RATE_LIMITED: 0,
        Outcome.INSUFFICIENT_QUOTA: 0,
        Outcome.ERROR: 0,
    }


def test_token_summary_over_no_samples_is_empty_not_zero() -> None:
    """No measurement is ``None``, which is not the same claim as zero."""
    summary = TokenSummary.from_counts([])
    assert summary.count == 0
    assert summary.p50 is None
    assert summary.p95 is None
    assert summary.mean is None
    assert summary.max is None


def test_token_summary_uses_the_same_nearest_rank_percentiles() -> None:
    """p50 and p95 are observed samples, exactly as for latency."""
    summary = TokenSummary.from_counts([10, 20, 30, 40, 50, 60, 70, 80, 90, 100])
    assert summary.count == 10
    assert summary.p50 == percentile_nearest_rank(
        [10, 20, 30, 40, 50, 60, 70, 80, 90, 100], P50
    )
    assert summary.p95 == percentile_nearest_rank(
        [10, 20, 30, 40, 50, 60, 70, 80, 90, 100], P95
    )
    assert summary.mean == 55.0
    assert summary.max == 100


def test_token_usage_summary_counts_only_successful_reported_trials() -> None:
    """A failure cannot pull a token percentile, and an unreported trial is
    counted as unreported rather than as a zero."""
    trials = [
        _trial(1.0, prompt_tokens=100, output_tokens=10, max_output_tokens=128),
        _trial(1.0, prompt_tokens=300, output_tokens=30, max_output_tokens=128),
        _trial(1.0),
        _trial(1.0, Outcome.TIMEOUT, prompt_tokens=1, output_tokens=1),
    ]
    summary = TokenUsageSummary.from_trials(trials)
    assert summary.reported_count == 2
    assert summary.unreported_count == 1
    assert summary.prompt.count == 2
    assert summary.prompt.max == 300
    assert summary.output.max == 30
    assert summary.output_cap_exceeded_count == 0


def test_token_usage_summary_counts_a_cap_overrun() -> None:
    """A completion past the budget reflect asked for is reported, not clipped."""
    trials = [
        _trial(1.0, prompt_tokens=100, output_tokens=900, max_output_tokens=128),
        _trial(1.0, prompt_tokens=100, output_tokens=128, max_output_tokens=128),
    ]
    summary = TokenUsageSummary.from_trials(trials)
    assert summary.output_cap_exceeded_count == 1


def test_token_usage_summary_over_no_trials_is_empty() -> None:
    """An empty group summarises to nothing measured, and does not raise."""
    summary = TokenUsageSummary.from_trials([])
    assert summary.reported_count == 0
    assert summary.unreported_count == 0
    assert summary.prompt.p50 is None


def test_a_trial_reporting_only_one_counter_still_counts_as_reported() -> None:
    """A runtime may send one counter and omit the other.

    It feeds one of the summaries either way, so counting it as *unreported*
    would let ``output.count`` exceed ``reported_count`` — the opposite of what
    ``unreported_count`` is documented to mean.
    """
    summary = TokenUsageSummary.from_trials(
        [_trial(1.0, output_tokens=42, max_output_tokens=128)]
    )
    assert summary.reported_count == 1
    assert summary.unreported_count == 0
    assert summary.prompt.count == 0
    assert summary.output.count == 1
    assert summary.output.count <= summary.reported_count
