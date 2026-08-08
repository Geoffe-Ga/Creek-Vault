"""Tests for the cluster-size guardrail (:mod:`creek.link.cluster_limits`).

These tests pin the *guardrail*, not the cure. The cure for a degenerate
mega-cluster is naming plus stream segmentation; this module only proves
that a size ceiling holds even when those fail, that the recursive split
terminates on BOTH of its bounds, and that anything still oversized when
the budget runs out is discarded to noise loudly rather than silently
materialised as a page.

Scale-sensitive cases construct :class:`SplitPolicy` with an explicit
small ``size_ceiling`` so the *fractional* term governs. Do not
"simplify" them back to the defaults: ``size_ceiling=500`` exceeds every
corpus a unit test can afford to build, so the absolute floor would
dominate and every assertion would pass vacuously.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import pytest

from creek.link.cluster_limits import (
    Recluster,
    SplitPolicy,
    Tightening,
    split_oversized,
)

if TYPE_CHECKING:
    from collections.abc import Sequence


class _Recorder:
    """Records every ``(members, parameter)`` pair a splitter re-clusters."""

    def __init__(self) -> None:
        """Initialise an empty call log."""
        self.calls: list[tuple[tuple[str, ...], float]] = []

    def record(self, members: Sequence[str], parameter: float) -> None:
        """Log one re-clustering call.

        Args:
            members: The cluster handed to the re-clusterer.
            parameter: The tightened clustering parameter used.
        """
        self.calls.append((tuple(members), parameter))

    @property
    def parameters(self) -> list[float]:
        """Return the tightened parameter of every recorded call."""
        return [parameter for _members, parameter in self.calls]


def _ids(count: int, prefix: str = "f") -> list[str]:
    """Return *count* synthetic fragment IDs.

    Args:
        count: Number of IDs to build.
        prefix: Prefix for each generated ID.

    Returns:
        A list of ``count`` distinct fragment IDs.
    """
    return [f"{prefix}-{index:04d}" for index in range(count)]


def _halving(recorder: _Recorder) -> Recluster:
    """Return a re-clusterer that halves every cluster it is given.

    Args:
        recorder: Call log to append to.

    Returns:
        A callable with the ``recluster`` contract that splits a cluster
        into two equal halves (a singleton is returned unchanged).
    """

    def recluster(members: Sequence[str], parameter: float) -> list[list[str]]:
        recorder.record(members, parameter)
        midpoint = len(members) // 2
        if midpoint == 0:
            return [list(members)]
        return [list(members[:midpoint]), list(members[midpoint:])]

    return recluster


def _never_splits(recorder: _Recorder) -> Recluster:
    """Return a re-clusterer that hands the same cluster straight back.

    Args:
        recorder: Call log to append to.

    Returns:
        A callable with the ``recluster`` contract that never separates
        anything — the continuous-manifold case the ceiling exists for.
    """

    def recluster(members: Sequence[str], parameter: float) -> list[list[str]]:
        recorder.record(members, parameter)
        return [list(members)]

    return recluster


class TestSplitPolicy:
    """The ceiling arithmetic and its config projection."""

    def test_ceiling_for_uses_the_absolute_floor_on_small_corpora(self) -> None:
        """The absolute ``size_ceiling`` dominates below the crossover."""
        policy = SplitPolicy(size_ceiling=500, max_fraction=0.10, max_depth=3)
        assert policy.ceiling_for(12) == 500

    def test_ceiling_for_uses_the_fraction_at_scale(self) -> None:
        """The fractional term dominates on a demo-scale corpus."""
        policy = SplitPolicy(size_ceiling=500, max_fraction=0.10, max_depth=3)
        assert policy.ceiling_for(35330) == 3533

    def test_ceiling_for_full_fraction_is_the_documented_opt_out(self) -> None:
        """``max_fraction=1.0`` lets a cluster hold the whole corpus."""
        policy = SplitPolicy(size_ceiling=500, max_fraction=1.0, max_depth=3)
        assert policy.ceiling_for(1000) == 1000

    def test_from_linking_config_reads_the_three_cluster_knobs(self) -> None:
        """The config projection maps each knob to its policy field."""

        class _Config:
            cluster_size_ceiling = 42
            cluster_max_fraction = 0.25
            cluster_split_max_depth = 7

        policy = SplitPolicy.from_linking_config(_Config())
        assert policy == SplitPolicy(
            size_ceiling=42,
            max_fraction=0.25,
            max_depth=7,
        )

    @pytest.mark.parametrize(
        ("ceiling", "fraction", "depth"),
        [
            (0, 0.10, 3),
            (500, 0.0, 3),
            (500, 1.5, 3),
            (500, 0.10, -1),
        ],
    )
    def test_rejects_an_out_of_range_field(
        self,
        ceiling: int,
        fraction: float,
        depth: int,
    ) -> None:
        """Out-of-range policy fields fail fast at construction.

        Args:
            ceiling: Candidate ``size_ceiling``.
            fraction: Candidate ``max_fraction``.
            depth: Candidate ``max_depth``.
        """
        with pytest.raises(ValueError, match=r"size_ceiling|max_fraction|max_depth"):
            SplitPolicy(
                size_ceiling=ceiling,
                max_fraction=fraction,
                max_depth=depth,
            )


class TestTightening:
    """The parameter schedule that bounds re-clustering independently."""

    def test_eddy_schedule_stops_before_eps_reaches_zero(self) -> None:
        """Stepping eps down from 0.3 by 0.05 yields five valid rounds."""
        schedule = Tightening.for_eddy_eps(0.3, 0.05).schedule(20)
        assert len(schedule) == 5
        assert all(value > 0.0 for value in schedule)
        assert schedule[0] == pytest.approx(0.25)

    def test_thread_schedule_stops_before_similarity_reaches_one(self) -> None:
        """Stepping similarity up from 0.6 by 0.1 yields three rounds."""
        schedule = Tightening.for_thread_similarity(0.6, 0.1).schedule(20)
        assert len(schedule) == 3
        assert all(value < 1.0 for value in schedule)
        assert schedule[0] == pytest.approx(0.7)

    def test_schedule_is_bounded_by_max_rounds(self) -> None:
        """The depth budget truncates a schedule with range to spare."""
        assert Tightening.for_eddy_eps(0.3, 0.05).schedule(2) == pytest.approx(
            [0.25, 0.20]
        )

    def test_schedule_is_empty_with_no_depth_budget(self) -> None:
        """A zero depth budget permits no re-clustering rounds at all."""
        assert Tightening.for_thread_similarity(0.6, 0.1).schedule(0) == []

    @pytest.mark.parametrize("step", [0.0, -0.1])
    def test_rejects_a_step_that_does_not_tighten(self, step: float) -> None:
        """A zero or wrong-signed step is rejected at construction.

        Args:
            step: Candidate step magnitude.
        """
        with pytest.raises(ValueError, match="step"):
            Tightening.for_thread_similarity(0.6, step)

    def test_rejects_a_start_outside_the_valid_range(self) -> None:
        """A start already at the limit leaves no room to tighten."""
        with pytest.raises(ValueError, match="start"):
            Tightening.for_thread_similarity(1.0, 0.1)


class TestSplitOversized:
    """The bounded recursive splitter and its discard path."""

    def test_cluster_within_the_ceiling_is_returned_untouched(self) -> None:
        """A cluster at or under the ceiling is never re-clustered."""
        recorder = _Recorder()
        members = _ids(8)
        result = split_oversized(
            members,
            ceiling=8,
            policy=SplitPolicy(size_ceiling=1, max_fraction=0.10, max_depth=3),
            recluster=_halving(recorder),
            tightening=Tightening.for_eddy_eps(0.3, 0.05),
            domain="test-domain",
        )
        assert result.accepted == [members]
        assert result.discarded == []
        assert recorder.calls == []

    def test_empty_membership_yields_nothing(self) -> None:
        """An empty cluster produces no accepted cluster and no noise."""
        result = split_oversized(
            [],
            ceiling=4,
            policy=SplitPolicy(size_ceiling=1, max_fraction=0.10, max_depth=3),
            recluster=_halving(_Recorder()),
            tightening=Tightening.for_eddy_eps(0.3, 0.05),
            domain="test-domain",
        )
        assert result.accepted == []
        assert result.discarded == []

    def test_oversized_cluster_is_split_until_every_part_fits(self) -> None:
        """Recursion continues until no accepted cluster exceeds the bar."""
        recorder = _Recorder()
        members = _ids(16)
        result = split_oversized(
            members,
            ceiling=4,
            policy=SplitPolicy(size_ceiling=1, max_fraction=0.10, max_depth=3),
            recluster=_halving(recorder),
            tightening=Tightening.for_eddy_eps(0.3, 0.05),
            domain="test-domain",
        )
        assert result.discarded == []
        assert all(len(cluster) <= 4 for cluster in result.accepted)
        assert sorted(fid for cluster in result.accepted for fid in cluster) == members
        # Each round tightens: 16 -> two 8s -> four 4s.
        assert recorder.parameters == pytest.approx([0.25, 0.20, 0.20])

    def test_split_terminates_when_the_tightened_parameter_leaves_its_valid_range(
        self,
    ) -> None:
        """Termination is governed by the parameter bound, not depth alone.

        With ``max_depth=20`` the depth cap cannot stop the recursion, so
        only the tightening schedule can: similarity stepping 0.1 from 0.6
        runs out of valid range before reaching 1.0. A depth-only guard
        would keep calling the re-clusterer at similarity >= 1.0, where
        nothing is ever topic-consistent and every cluster silently
        becomes noise.
        """
        recorder = _Recorder()
        members = _ids(64)
        result = split_oversized(
            members,
            ceiling=8,
            policy=SplitPolicy(size_ceiling=1, max_fraction=0.10, max_depth=20),
            recluster=_never_splits(recorder),
            tightening=Tightening.for_thread_similarity(0.6, 0.1),
            domain="test-domain",
        )
        assert len(recorder.calls) <= 4
        assert all(parameter < 1.0 for parameter in recorder.parameters)
        assert recorder.parameters == pytest.approx([0.7, 0.8, 0.9])
        assert result.accepted == []
        assert result.discarded == members

    def test_zero_depth_budget_discards_an_oversized_cluster_immediately(self) -> None:
        """With no split budget an oversized cluster goes straight to noise."""
        recorder = _Recorder()
        members = _ids(20)
        result = split_oversized(
            members,
            ceiling=4,
            policy=SplitPolicy(size_ceiling=1, max_fraction=0.10, max_depth=0),
            recluster=_halving(recorder),
            tightening=Tightening.for_eddy_eps(0.3, 0.05),
            domain="test-domain",
        )
        assert recorder.calls == []
        assert result.accepted == []
        assert result.discarded == members

    def test_unsplittable_cluster_warns_with_its_domain_and_size(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """The discard is loud: one WARNING naming the domain and size.

        Args:
            caplog: Pytest log capture fixture.
        """
        members = _ids(30)
        with caplog.at_level(logging.WARNING, logger="creek.link.cluster_limits"):
            split_oversized(
                members,
                ceiling=5,
                policy=SplitPolicy(size_ceiling=1, max_fraction=0.10, max_depth=2),
                recluster=_never_splits(_Recorder()),
                tightening=Tightening.for_eddy_eps(0.3, 0.05),
                domain="discord/Aspiring Drag Queens",
            )
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        message = warnings[0].getMessage()
        assert "discord/Aspiring Drag Queens" in message
        assert "30" in message

    def test_recluster_returning_nothing_discards_the_whole_cluster(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A cluster dissolved entirely to noise is discarded and warned.

        Args:
            caplog: Pytest log capture fixture.
        """
        members = _ids(12)

        def _dissolves(_members: Sequence[str], _parameter: float) -> list[list[str]]:
            return []

        with caplog.at_level(logging.WARNING, logger="creek.link.cluster_limits"):
            result = split_oversized(
                members,
                ceiling=4,
                policy=SplitPolicy(size_ceiling=1, max_fraction=0.10, max_depth=3),
                recluster=_dissolves,
                tightening=Tightening.for_eddy_eps(0.3, 0.05),
                domain="test-domain",
            )
        assert result.accepted == []
        assert result.discarded == members
        assert len(caplog.records) == 1

    def test_members_dropped_by_reclustering_are_reported_as_discarded(self) -> None:
        """Members the re-clusterer drops to noise still show up as noise."""
        members = _ids(12)

        def _keeps_half(sub: Sequence[str], _parameter: float) -> list[list[str]]:
            return [list(sub[: len(sub) // 2])]

        result = split_oversized(
            members,
            ceiling=4,
            policy=SplitPolicy(size_ceiling=1, max_fraction=0.10, max_depth=3),
            recluster=_keeps_half,
            tightening=Tightening.for_eddy_eps(0.3, 0.05),
            domain="test-domain",
        )
        assert result.accepted == [members[:3]]
        assert result.discarded == members[3:]

    def test_accepted_and_discarded_partition_the_input(self) -> None:
        """Every input ID lands in exactly one of accepted or discarded."""
        members = _ids(37)

        def _drops_the_tail(sub: Sequence[str], _parameter: float) -> list[list[str]]:
            head = list(sub[:-1])
            midpoint = len(head) // 2
            return [head[:midpoint], head[midpoint:]] if midpoint else [head]

        result = split_oversized(
            members,
            ceiling=5,
            policy=SplitPolicy(size_ceiling=1, max_fraction=0.10, max_depth=3),
            recluster=_drops_the_tail,
            tightening=Tightening.for_eddy_eps(0.3, 0.05),
            domain="test-domain",
        )
        placed = [fid for cluster in result.accepted for fid in cluster]
        assert sorted(placed + result.discarded) == members
        assert not set(placed) & set(result.discarded)

    def test_input_sequence_is_not_mutated(self) -> None:
        """The splitter never writes through to the caller's membership list."""
        members = _ids(16)
        original = list(members)
        split_oversized(
            members,
            ceiling=4,
            policy=SplitPolicy(size_ceiling=1, max_fraction=0.10, max_depth=3),
            recluster=_halving(_Recorder()),
            tightening=Tightening.for_eddy_eps(0.3, 0.05),
            domain="test-domain",
        )
        assert members == original

    def test_tightened_parameter_is_passed_never_assigned_to_the_detector(self) -> None:
        """Re-clustering overrides the parameter without mutating detector state.

        The splitter drives a detector through a callable that *receives*
        the tightened value. If it instead reached in and set the
        detector's own threshold, later unrelated detection on the same
        instance would silently run at a tightened parameter.
        """

        class _FakeDetector:
            """Minimal stand-in exposing a mutable clustering parameter."""

            def __init__(self) -> None:
                """Start at the module-default eddy epsilon."""
                self.eps = 0.3

            def recluster(
                self,
                sub: Sequence[str],
                parameter: float,
            ) -> list[list[str]]:
                """Cluster *sub* at *parameter* without touching ``self.eps``.

                Args:
                    sub: Member IDs to re-cluster.
                    parameter: Tightened epsilon for this round only.

                Returns:
                    Halves of *sub*, more finely split as eps tightens.
                """
                size = max(1, int(len(sub) * parameter / 0.3) // 2)
                return [list(sub[i : i + size]) for i in range(0, len(sub), size)]

        detector = _FakeDetector()
        split_oversized(
            _ids(16),
            ceiling=4,
            policy=SplitPolicy(size_ceiling=1, max_fraction=0.10, max_depth=3),
            recluster=detector.recluster,
            tightening=Tightening.for_eddy_eps(0.3, 0.05),
            domain="test-domain",
        )
        assert detector.eps == pytest.approx(0.3)
