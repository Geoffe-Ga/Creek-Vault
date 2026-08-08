"""Cluster-size guardrail — a bounded last line of defence, not the cure.

Density-based clustering has no size bound. DBSCAN propagates one label
across an entire density-connected component, and thread union-find takes
the transitive closure of every windowed pair. On a continuous message
stream — semantically homogeneous, temporally unbroken — a single
mega-cluster is not a badly chosen threshold, it is the algorithm's own
precondition (clusters separated by low-density regions) being violated.
No value of ``eps`` or ``similarity_threshold`` fixes that.

**This module is therefore not the fix, and must not be read as one.**
Recursively tightening a parameter on a 30k-member continuous semantic
manifold shatters it into arbitrary slices with no interpretable identity
— trading one meaningless page for sixty. The cure is *naming* (a term
carried by most of the corpus can never become a title) and *segmentation*
(never build one epsilon-graph over seven years of chat in the first
place). What this module supplies is the guarantee that holds when those
fail, or when a corpus nobody has looked at yet degenerates in a way
nobody predicted:

    no emitted cluster exceeds ``policy.ceiling_for(corpus_size)``

That is the machine-checkable invariant behind the acceptance criterion
"no single eddy or thread contains more than the configured ceiling
fraction of the corpus". It is a floor on badness, not a definition of
goodness.

Placement is deliberate. Every function here operates on *membership
lists a detector has already produced*. Nothing in this module calls,
wraps, patches or perturbs DBSCAN's neighbour discovery or its cluster
expansion, so the equivalence contract that pins ``_dbscan`` against an
independent brute-force reference (``test_dbscan_matches_brute_force_reference``)
is untouched: that oracle compares raw ``_dbscan`` output, which this
guardrail never sees and never alters. Re-clustering is likewise not
performed here — the caller injects a ``recluster`` callable that takes
the tightened parameter as an *argument*, so no detector's own state is
ever mutated mid-run.

Termination is governed by TWO independent bounds, and both are load
bearing:

1. :attr:`SplitPolicy.max_depth` — the recursion budget.
2. :class:`Tightening` running out of valid range — epsilon may not reach
   ``0.0``, similarity may not reach ``1.0``. A depth-only guard would
   keep re-clustering at a degenerate parameter where nothing is ever a
   neighbour and every member silently becomes noise.

A cluster still over the ceiling once both are exhausted is discarded to
noise: its members lose their eddy/thread frontmatter links entirely. That
is a real cost, accepted because an unusable mega-page is worse than no
page, and because with segmentation in place the path should be
unreachable. It is never silent — each discard logs a WARNING naming the
domain and the size, and the discarded IDs come back to the caller so an
operator-visible count can be surfaced.
"""

from __future__ import annotations

import logging
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from itertools import chain
from typing import NamedTuple, Protocol

logger = logging.getLogger(__name__)

_EPS_FLOOR = 0.0
"""Epsilon may be tightened towards, but never to, zero distance."""

_SIMILARITY_CEILING = 1.0
"""Similarity may be tightened towards, but never to, exact identity."""

Recluster = Callable[[Sequence[str], float], list[list[str]]]
"""Re-cluster *members* at a tightened parameter, returning subclusters.

The callable must treat the parameter as a per-call override and must
return clusters drawn from the members it was handed. Any member it omits
(DBSCAN noise, a component below the minimum size) is counted as noise by
:func:`split_oversized` and reported as discarded.
"""


class SplitPolicySource(Protocol):
    """Structural view of the linking-config fields a policy is built from.

    Declared structurally rather than importing the concrete config class,
    so the guardrail stays a pure, dependency-free module.
    """

    @property
    def cluster_size_ceiling(self) -> int:
        """Absolute member count below which a cluster is never split."""

    @property
    def cluster_max_fraction(self) -> float:
        """Largest share of the corpus one cluster may hold."""

    @property
    def cluster_split_max_depth(self) -> int:
        """Maximum number of re-clustering rounds per oversized cluster."""


class SplitResult(NamedTuple):
    """The outcome of splitting one oversized cluster.

    ``accepted`` and ``discarded`` partition the input membership: every
    ID handed to :func:`split_oversized` appears in exactly one of them.
    """

    accepted: list[list[str]]
    """Subclusters that fit under the ceiling, in breadth-first order."""

    discarded: list[str]
    """Member IDs that ended up as noise, in input order."""


@dataclass(frozen=True, slots=True)
class SplitPolicy:
    """How large a cluster may be, and how hard to try to shrink it.

    The ceiling is the larger of an absolute floor and a fraction of the
    corpus. The absolute floor dominates small corpora — so ordinary
    vaults and unit-test fixtures never trip the guardrail at all — while
    the fraction dominates at scale, which is where degeneration shows up.

    Attributes:
        size_ceiling: Absolute member count below which no cluster is ever
            split, regardless of corpus size. Must be at least 1.
        max_fraction: Largest share of the corpus a single cluster may
            hold. Must be greater than 0 and at most 1; ``1.0`` is the
            documented opt-out, permitting a cluster to span everything.
        max_depth: Re-clustering rounds allowed per oversized cluster.
            ``0`` disables splitting, sending any oversized cluster
            straight to noise.
    """

    size_ceiling: int = 500
    max_fraction: float = 0.10
    max_depth: int = 3

    def __post_init__(self) -> None:
        """Validate the policy fields.

        Raises:
            ValueError: If any field falls outside its documented range.
        """
        if self.size_ceiling < 1:
            msg = f"size_ceiling must be >= 1, got {self.size_ceiling}"
            raise ValueError(msg)
        if not 0.0 < self.max_fraction <= 1.0:
            msg = f"max_fraction must be in (0, 1], got {self.max_fraction}"
            raise ValueError(msg)
        if self.max_depth < 0:
            msg = f"max_depth must be >= 0, got {self.max_depth}"
            raise ValueError(msg)

    def ceiling_for(self, corpus_size: int) -> int:
        """Return the largest permitted cluster size for a corpus.

        Args:
            corpus_size: Number of clusterable fragments in the corpus.

        Returns:
            ``max(size_ceiling, floor(corpus_size * max_fraction))``.
        """
        return max(self.size_ceiling, int(corpus_size * self.max_fraction))

    @classmethod
    def from_linking_config(cls, config: SplitPolicySource) -> SplitPolicy:
        """Build a policy from the linking configuration.

        Args:
            config: Any object exposing the cluster-limit knobs — in
                practice ``CreekConfig.linking``.

        Returns:
            The policy those settings describe.
        """
        return cls(
            size_ceiling=config.cluster_size_ceiling,
            max_fraction=config.cluster_max_fraction,
            max_depth=config.cluster_split_max_depth,
        )


@dataclass(frozen=True, slots=True)
class Tightening:
    """A monotone schedule of progressively stricter clustering parameters.

    Each round moves *start* one *step* closer to *limit*. The limit is
    exclusive: it is the value at which the parameter stops meaning
    anything (zero epsilon admits no neighbours; unit similarity admits no
    pair), so a schedule stops strictly before reaching it. This bound is
    independent of the depth budget, and both must hold.

    Attributes:
        start: The detector's configured parameter — never used for
            re-clustering itself, only as the point the schedule steps
            away from.
        step: Signed increment applied each round. Negative tightens
            downwards (epsilon), positive upwards (similarity).
        limit: Exclusive bound the parameter may approach but not reach.
    """

    start: float
    step: float
    limit: float

    def __post_init__(self) -> None:
        """Validate that the schedule actually tightens towards its limit.

        Raises:
            ValueError: If the step is zero, points away from the limit,
                or the start already sits at or beyond the limit.
        """
        if self.step == 0.0:
            msg = "step must be non-zero"
            raise ValueError(msg)
        if self.start == self.limit:
            msg = f"start {self.start} sits at the limit, leaving no room to tighten"
            raise ValueError(msg)
        if (self.step > 0.0) != (self.start < self.limit):
            msg = (
                f"step {self.step} does not tighten {self.start} "
                f"towards limit {self.limit}"
            )
            raise ValueError(msg)

    def _within_range(self, value: float) -> bool:
        """Return whether *value* is still a usable clustering parameter.

        Args:
            value: A candidate tightened parameter.

        Returns:
            ``True`` while *value* remains strictly on the starting side
            of :attr:`limit`.
        """
        if self.step > 0.0:
            return value < self.limit
        return value > self.limit

    def schedule(self, max_rounds: int) -> list[float]:
        """Return the tightened parameters for up to *max_rounds* rounds.

        Values are computed as ``start + step * round`` rather than by
        repeated addition, so float drift cannot accumulate into an extra
        (or missing) round.

        Args:
            max_rounds: Depth budget from :attr:`SplitPolicy.max_depth`.

        Returns:
            Successively tighter parameters, truncated by whichever bound
            bites first — the budget or the valid range. May be empty.
        """
        values: list[float] = []
        for round_index in range(1, max_rounds + 1):
            value = self.start + self.step * round_index
            if not self._within_range(value):
                break
            values.append(value)
        return values

    @classmethod
    def for_eddy_eps(cls, eps: float, step: float) -> Tightening:
        """Build the schedule that tightens eddy DBSCAN epsilon downwards.

        Args:
            eps: The detector's configured epsilon (cosine distance).
            step: Positive magnitude to subtract each round.

        Returns:
            A schedule stepping *eps* down towards, but never reaching,
            zero distance.
        """
        return cls(start=eps, step=-step, limit=_EPS_FLOOR)

    @classmethod
    def for_thread_similarity(cls, threshold: float, step: float) -> Tightening:
        """Build the schedule that tightens thread similarity upwards.

        Args:
            threshold: The detector's configured cosine-similarity floor.
            step: Positive magnitude to add each round.

        Returns:
            A schedule stepping *threshold* up towards, but never
            reaching, exact identity.
        """
        return cls(start=threshold, step=step, limit=_SIMILARITY_CEILING)


def _discard(domain: str, size: int, ceiling: int, rounds: int) -> None:
    """Log the discard of a cluster that could not be brought under the bar.

    Args:
        domain: Clustering domain the cluster came from, for the operator.
        size: Member count of the discarded cluster.
        ceiling: Ceiling it failed to meet.
        rounds: Re-clustering rounds already spent on it.
    """
    logger.warning(
        "Discarding oversized cluster to noise: domain=%s size=%d "
        "still exceeds the %d-fragment ceiling after %d re-clustering round(s); "
        "its members will carry no eddy or thread link",
        domain,
        size,
        ceiling,
        rounds,
    )


def _noise(members: Sequence[str], accepted: list[list[str]]) -> list[str]:
    """Return the input members that no accepted subcluster kept.

    Args:
        members: The original membership, in input order.
        accepted: Subclusters that survived the split.

    Returns:
        Every member ID absent from *accepted*, in input order.
    """
    kept = set(chain.from_iterable(accepted))
    return [fid for fid in members if fid not in kept]


def split_oversized(
    members: Sequence[str],
    *,
    ceiling: int,
    policy: SplitPolicy,
    recluster: Recluster,
    tightening: Tightening,
    domain: str,
) -> SplitResult:
    """Split a cluster until every part fits under *ceiling*.

    Applies bounded recursive re-clustering entirely outside the
    detectors' own clustering routines: it only ever re-partitions a
    membership list that has already been produced. Recursion is driven by
    an explicit work queue rather than nested calls, and stops on
    whichever bound bites first — the depth budget or the tightening
    schedule leaving its valid range.

    Args:
        members: Member IDs of one already-detected cluster.
        ceiling: Largest permitted cluster size, from
            :meth:`SplitPolicy.ceiling_for`.
        policy: Supplies the depth budget.
        recluster: Callable re-partitioning a membership at a tightened
            parameter. See :data:`Recluster` for its contract.
        tightening: Parameter schedule for successive rounds.
        domain: Clustering domain the cluster belongs to, named in the
            WARNING if it has to be discarded.

    Returns:
        A :class:`SplitResult` whose ``accepted`` and ``discarded`` fields
        partition *members*.
    """
    if not members:
        return SplitResult([], [])
    schedule = tightening.schedule(policy.max_depth)
    accepted: list[list[str]] = []
    pending = deque([(list(members), 0)])
    while pending:
        cluster, depth = pending.popleft()
        if len(cluster) <= ceiling:
            accepted.append(cluster)
            continue
        if depth >= len(schedule):
            _discard(domain, len(cluster), ceiling, depth)
            continue
        subclusters = recluster(cluster, schedule[depth])
        if not subclusters:
            _discard(domain, len(cluster), ceiling, depth + 1)
            continue
        pending.extend((sub, depth + 1) for sub in subclusters)
    return SplitResult(accepted, _noise(members, accepted))
