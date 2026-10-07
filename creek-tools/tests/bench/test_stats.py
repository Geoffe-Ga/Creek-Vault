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
    error_rate,
    percentile_nearest_rank,
)
from creek_mcp.bench.trial import Phase, Sweep, Trial


def _trial(latency_s: float, outcome: Outcome = Outcome.OK) -> Trial:
    """Build one warm, serial trial with *latency_s* and *outcome*."""
    return Trial(
        sweep=Sweep.COLD_WARM,
        phase=Phase.WARM,
        concurrency=1,
        input_words=8,
        latency_s=latency_s,
        generation_s=None,
        outcome=outcome,
        model_resident_bytes=None,
    )


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
        Outcome.ERROR: 0,
    }
