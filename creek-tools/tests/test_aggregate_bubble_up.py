"""Tests for the aggregate-path weighted bubble-up."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from creek.atomize.aggregate import AggregationConfig, aggregate
from creek.classify.weighted import (
    WeightedDimension,
    WeightedFragmentClassification,
)
from creek.models import (
    Fragment,
    FragmentSource,
    Frequency,
    Phase,
    SourcePlatform,
)

# ---- Fixture helpers -------------------------------------------------------


_BASE_TIME = datetime(2026, 1, 1, 9, 0, tzinfo=UTC)


def _wfc(
    *,
    frequencies: tuple[tuple[Frequency, float], ...] = (),
    phases: tuple[tuple[Phase, float], ...] = (),
    overall_confidence: float = 0.7,
) -> WeightedFragmentClassification:
    """Build a :class:`WeightedFragmentClassification` from concise tuples."""
    return WeightedFragmentClassification(
        frequencies=tuple(WeightedDimension(value=v, weight=w) for v, w in frequencies),
        phases=tuple(WeightedDimension(value=v, weight=w) for v, w in phases),
        overall_confidence=overall_confidence,
    )


def _message(
    idx: int,
    *,
    weighted: WeightedFragmentClassification | None = None,
    minutes_offset: int = 0,
) -> Fragment:
    """Build a chat-style :class:`Fragment` at level ``document``."""
    return Fragment(
        id=f"frag-msg00{idx:09d}",
        title=f"Message {idx}",
        source=FragmentSource(
            platform=SourcePlatform.DISCORD,
            channel="#testing",
            conversation_id="conv-1",
        ),
        created=_BASE_TIME + timedelta(minutes=minutes_offset),
        level="document",
        weighted=weighted,
    )


# ---- _build_parent populates weighted on aggregated parent -----------------


class TestAggregateBubbleUpHappyPaths:
    """When children carry signal, the aggregated parent reflects it."""

    def test_five_agreeing_messages_produce_f2_dominant_exchange(self) -> None:
        """Five high-confidence F2 chats aggregate into an F2-dominant exchange."""
        children = [
            _message(
                i,
                weighted=_wfc(
                    frequencies=((Frequency.F2, 0.8),),
                    overall_confidence=0.85,
                ),
                minutes_offset=i,
            )
            for i in range(5)
        ]
        parents = aggregate(children, level="exchange", config=AggregationConfig())
        assert len(parents) == 1
        parent_weighted = parents[0].weighted
        assert parent_weighted is not None
        assert parent_weighted.frequencies[0].value is Frequency.F2

    def test_five_divergent_messages_dampens_confidence(self) -> None:
        """Confident messages across F1/F3/F5/F7/F9 dampen the parent confidence."""
        diverged_freqs = (
            Frequency.F1,
            Frequency.F3,
            Frequency.F5,
            Frequency.F7,
            Frequency.F9,
        )
        children = [
            _message(
                i,
                weighted=_wfc(
                    frequencies=((freq, 0.9),),
                    overall_confidence=0.9,
                ),
                minutes_offset=i,
            )
            for i, freq in enumerate(diverged_freqs)
        ]
        parents = aggregate(children, level="exchange", config=AggregationConfig())
        assert len(parents) == 1
        weighted = parents[0].weighted
        assert weighted is not None
        assert weighted.overall_confidence < 0.9


# ---- The user's load-bearing case ------------------------------------------


class TestConvictionOverLengthAggregate:
    """One confident-short F3 message beats four hedged-long F5 messages."""

    def test_confident_short_beats_hedged_long_in_aggregation(self) -> None:
        """Load-bearing fixture for #369 — see PR conversation."""
        confident_short = _message(
            0,
            weighted=_wfc(
                frequencies=((Frequency.F3, 0.9),),
                overall_confidence=0.9,
            ),
            minutes_offset=0,
        )
        hedged_long = [
            _message(
                i + 1,
                weighted=_wfc(
                    frequencies=((Frequency.F5, 0.3),),
                    overall_confidence=0.2,
                ),
                minutes_offset=i + 1,
            )
            for i in range(4)
        ]
        parents = aggregate(
            [confident_short, *hedged_long],
            level="exchange",
            config=AggregationConfig(),
        )
        assert len(parents) == 1
        weighted = parents[0].weighted
        assert weighted is not None
        assert weighted.frequencies[0].value is Frequency.F3


# ---- The fill-floor structural gate ----------------------------------------


class TestFillFloorGate:
    """``weighted_fill_floor`` filters thinly-classified aggregations."""

    def test_below_fill_floor_produces_none(self) -> None:
        """Fill fraction below the default 0.5 floor → parent.weighted is None.

        Two of five children carry ``weighted``; that's 40% fill,
        below the default ``weighted_fill_floor=0.5``, so the parent
        stays ``None`` rather than carrying a thinly-sourced profile.
        """
        children = [
            _message(0, weighted=_wfc(frequencies=((Frequency.F2, 0.8),))),
            _message(1, weighted=_wfc(frequencies=((Frequency.F2, 0.8),))),
            _message(2, weighted=None, minutes_offset=2),
            _message(3, weighted=None, minutes_offset=3),
            _message(4, weighted=None, minutes_offset=4),
        ]
        parents = aggregate(children, level="exchange", config=AggregationConfig())
        assert parents[0].weighted is None

    def test_majority_weighted_passes_the_gate(self) -> None:
        """3 of 5 children weighted → parent.weighted is non-None."""
        children = [
            _message(
                0,
                weighted=_wfc(frequencies=((Frequency.F2, 0.8),)),
                minutes_offset=0,
            ),
            _message(
                1,
                weighted=_wfc(frequencies=((Frequency.F2, 0.8),)),
                minutes_offset=1,
            ),
            _message(
                2,
                weighted=_wfc(frequencies=((Frequency.F2, 0.8),)),
                minutes_offset=2,
            ),
            _message(3, weighted=None, minutes_offset=3),
            _message(4, weighted=None, minutes_offset=4),
        ]
        parents = aggregate(children, level="exchange", config=AggregationConfig())
        assert parents[0].weighted is not None

    def test_all_unweighted_produces_none(self) -> None:
        """Every child unweighted → no fabrication."""
        children = [_message(i, weighted=None, minutes_offset=i) for i in range(3)]
        parents = aggregate(children, level="exchange", config=AggregationConfig())
        assert parents[0].weighted is None


# ---- Zero-confidence pass-through (signal-side gate via #367 invariant) ----


class TestZeroConfidencePassThrough:
    """Zero-confidence children contribute nothing to the combine."""

    def test_zero_confidence_children_passed_floor_yield_no_signal(self) -> None:
        """All weighted but ``overall_confidence=0`` → parent profile empty."""
        children = [
            _message(
                i,
                weighted=_wfc(
                    frequencies=((Frequency.F2, 0.8),),
                    overall_confidence=0.0,
                ),
                minutes_offset=i,
            )
            for i in range(3)
        ]
        parents = aggregate(children, level="exchange", config=AggregationConfig())
        weighted = parents[0].weighted
        # The fill-floor gate accepts (all children have non-None
        # weighted), but the combiner returns the empty WFC because
        # every child carries zero confidence.
        assert weighted is not None
        assert weighted.frequencies == ()
        assert weighted.overall_confidence == 0.0


# ---- Single-child aggregation is idempotent (combiner invariant) -----------


class TestSingleChildAggregation:
    """A one-child aggregation reproduces the child's weighted profile."""

    def test_single_message_aggregation_mirrors_child(self) -> None:
        """Idempotence on N=1 holds through the wiring."""
        child = _message(
            0,
            weighted=_wfc(
                frequencies=((Frequency.F3, 0.8),),
                phases=((Phase.RISING, 0.7),),
                overall_confidence=0.85,
            ),
        )
        parents = aggregate([child], level="exchange", config=AggregationConfig())
        parent = parents[0]
        assert parent.weighted is not None
        assert parent.weighted.frequencies[0].value is Frequency.F3
        assert parent.weighted.overall_confidence == pytest.approx(0.85)


# ---- Idempotence of aggregate() with weighted children ---------------------


class TestAggregateIdempotence:
    """Re-running aggregate produces parents with identical ``weighted`` fields."""

    def test_re_aggregate_produces_byte_identical_weighted(self) -> None:
        """FEAT-022 idempotence holds for the new ``weighted`` field too."""
        children = [
            _message(
                i,
                weighted=_wfc(
                    frequencies=((Frequency.F2, 0.8),),
                    overall_confidence=0.85,
                ),
                minutes_offset=i,
            )
            for i in range(5)
        ]
        first = aggregate(children, level="exchange", config=AggregationConfig())
        second = aggregate(children, level="exchange", config=AggregationConfig())
        # Both runs produce one parent each; their ``weighted`` is equal.
        assert first[0].weighted == second[0].weighted
