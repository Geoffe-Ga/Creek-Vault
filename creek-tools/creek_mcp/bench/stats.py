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
        outcome_counts: Trials per outcome, every outcome present.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    count: NonNegativeInt
    ok_count: NonNegativeInt
    error_rate: NonNegativeFloat
    p50_s: NonNegativeFloat | None
    p95_s: NonNegativeFloat | None
    generation_p95_s: NonNegativeFloat | None
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
            outcome_counts=counts,
        )
