"""Holonic combine / decompose math for weighted classifications (#367).

This module is the heart of epic #364: the pure-math operators that
combine a set of children's weighted classifications upward into a
parent. The split (#368) and aggregate (#369) sub-issues call into
:func:`combine` from the FEAT-023 reatomize orchestrator and the
FEAT-022 aggregator respectively; this module performs no IO, no LLM
calls, no file reads — just numerical aggregation over the dataclasses
landed by #365.

Why not length
==============

Length is incidental to ontological mood. A 3-word phrase ("Yes, I'm
rising.") confidently asserting F2/rising should outweigh a 500-word
ramble dimly hedging restoration. So:

- A child's *influence* on its parent defaults to its own
  :attr:`overall_confidence` — the model's self-rated reliability of
  the whole classification — not its token count.
- The *per-entry conviction* (:attr:`WeightedDimension.weight`) is the
  model's strength for one specific value; the parent's weight on
  that value is a confidence-weighted average of per-entry convictions
  across children.
- Zero-confidence children contribute nothing — useful when a child
  classifier failed soft to an empty profile, so an empty entry does
  not pollute the aggregate.

Callers that need an external prior on importance (heading-prominent
atoms, hand-curated boosts) can override the defaults via
:func:`combine`'s ``weights`` parameter; the default contract is
conviction x confidence and stays length-agnostic.

Confidence and divergence
=========================

Parent ``overall_confidence`` = ``confidence_weighted_mean x (1 -
divergence_penalty x jsd_norm)`` where:

- ``confidence_weighted_mean`` = ``sum_i(c_i x c_i) / sum_i(c_i)``.
  Children with higher confidence pull the mean toward themselves —
  a chorus of hedges does not manufacture parent confidence.
- ``jsd_norm`` = mean Jensen-Shannon divergence across per-dimension
  distributions, normalised to [0, 1]. Two hedged guesses pointing
  different ways produce low JSD (they are hedging, not
  disagreeing); two confident children pointing different ways
  produce high JSD.

Calibration of raw ``overall_confidence`` (FEAT-017 territory) is out
of scope here — the combiner assumes the confidences it receives are
already on a meaningful scale; if they are not, the fix lives in the
classifier (#366), not the combiner.

Limitations
===========

- Per-dimension confidence: today :attr:`overall_confidence` is a
  single global score. A future extension could give each dimension
  its own confidence (the model might be confident about Frequency
  but unsure about Mode). This module is structured so adding that
  would be a per-dimension change without touching call sites.
- Choosing the "intelligently sized basic unit" (the holarchy level
  to surface from a reatomized tree) lives in #368, not here. This
  module returns a single :class:`WeightedFragmentClassification`
  per ``combine`` call; the tree walk is the integrator's job.
"""

from __future__ import annotations

import math
from enum import StrEnum
from typing import TYPE_CHECKING, TypeVar

from creek.classify.weighted import (
    WeightedDimension,
    WeightedFragmentClassification,
)
from creek.models import (
    Dosage,
    Frequency,
    Mode,
    Orientation,
    Phase,
    VoiceRegister,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

__all__ = ["combine", "decompose_prior"]


_DimT = TypeVar("_DimT", bound=StrEnum)


# Per-dimension floor — entries with combined weight strictly below
# this fall off the parent's tuple. Matches the floor PR #359's
# template asks the LLM to honour, so the combiner does not surface
# values the model itself would have suppressed.
_DEFAULT_FLOOR: float = 0.05

# Cap on the per-child reasoning prefix size so a chatty leaf does
# not crowd out its siblings in the parent's truncated reasoning.
_REASONING_BUDGET_CHARS: int = 4000


def combine(
    children: Sequence[WeightedFragmentClassification],
    *,
    weights: Sequence[float] | None = None,
    top_k: int = 5,
    divergence_penalty: float = 0.5,
) -> WeightedFragmentClassification:
    """Combine children's weighted classifications into a parent's profile.

    The default per-child weight is :attr:`overall_confidence` — the
    model's self-rated reliability is the mass. Pass ``weights`` to
    override (e.g. for heading-prominent atoms or hand-curated
    boosts); the override is treated identically to confidence
    downstream, with the explicit reminder that **length is never the
    default mass**.

    Args:
        children: The children to roll up. Empty input produces an
            empty :class:`WeightedFragmentClassification`.
        weights: Optional per-child weight override aligned with
            ``children``. ``None`` defaults to
            ``[c.overall_confidence for c in children]``.
        top_k: Maximum entries kept per dimension on the parent.
        divergence_penalty: Coefficient applied to the normalised
            JSD when dampening parent confidence; ``0.0`` disables
            dampening, ``1.0`` zeroes confidence at maximal
            disagreement.

    Returns:
        The combined :class:`WeightedFragmentClassification`.

    Raises:
        ValueError: When ``weights`` is supplied with a length other
            than ``len(children)`` (the caller has a sequencing bug).
    """
    if not children:
        return WeightedFragmentClassification()

    effective_weights = _resolve_weights(children, weights)
    total = sum(effective_weights)
    if total <= 0:
        # No child carries any signal (or the explicit override zeros
        # everyone out). Return the empty classification — honest
        # "no signal" rather than fabricating one.
        return WeightedFragmentClassification(
            reasoning=_compose_reasoning(children, effective_weights),
        )

    # Per-dimension combines stay inline rather than going through a
    # heterogeneous-typed dict — mypy needs the per-dimension type
    # information to flow through unmolested, and the inline form
    # makes the symmetry across dimensions visually explicit.
    confidence = _combine_confidence(
        children,
        effective_weights,
        divergence_penalty=divergence_penalty,
    )
    reasoning = _compose_reasoning(children, effective_weights)
    return WeightedFragmentClassification(
        frequencies=_combine_dimension(
            [c.frequencies for c in children],
            effective_weights,
            top_k=top_k,
        ),
        phases=_combine_dimension(
            [c.phases for c in children],
            effective_weights,
            top_k=top_k,
        ),
        modes=_combine_dimension(
            [c.modes for c in children],
            effective_weights,
            top_k=top_k,
        ),
        orientations=_combine_dimension(
            [c.orientations for c in children],
            effective_weights,
            top_k=top_k,
        ),
        dosages=_combine_dimension(
            [c.dosages for c in children],
            effective_weights,
            top_k=top_k,
        ),
        voice_registers=_combine_dimension(
            [c.voice_registers for c in children],
            effective_weights,
            top_k=top_k,
        ),
        overall_confidence=confidence,
        reasoning=reasoning,
    )


def decompose_prior(
    parent: WeightedFragmentClassification,
    n_atoms: int,
) -> WeightedFragmentClassification:
    """Produce a soft prior atoms can use as starting classification context.

    Each per-dimension tuple flows through unchanged so atoms see the
    same canonical values the parent surfaced. The
    :attr:`overall_confidence` is halved so atoms have headroom to
    override when their own signal is stronger; ``n_atoms`` is
    accepted for future tuning (smaller siblings → weaker prior) but
    does not influence the current implementation. The returned
    instance is a soft *prior*, not an assertion: callers feed it as
    context to the per-atom classifier and let conviction override.

    Args:
        parent: The parent profile being decomposed.
        n_atoms: How many atoms the parent is being split into.
            Accepted today for forward compatibility; see the
            limitations section in the module docstring.

    Returns:
        A :class:`WeightedFragmentClassification` whose per-dimension
        tuples mirror the parent's and whose ``overall_confidence``
        is half the parent's (clamped to ``[0.0, 1.0]``).
    """
    del n_atoms  # accepted for forward compatibility; see docstring
    return WeightedFragmentClassification(
        frequencies=parent.frequencies,
        phases=parent.phases,
        modes=parent.modes,
        orientations=parent.orientations,
        dosages=parent.dosages,
        voice_registers=parent.voice_registers,
        overall_confidence=parent.overall_confidence * 0.5,
        reasoning=parent.reasoning,
    )


# ---- Weight resolution -----------------------------------------------------


def _resolve_weights(
    children: Sequence[WeightedFragmentClassification],
    weights: Sequence[float] | None,
) -> list[float]:
    """Resolve the per-child weight vector with the documented defaults.

    Clamps negative explicit overrides to zero so a misuse-by-typo
    does not invert rankings; ``None`` defaults to each child's
    :attr:`overall_confidence`.

    Raises:
        ValueError: On a length mismatch between ``weights`` and
            ``children`` — the caller has a sequencing bug.
    """
    if weights is None:
        return [max(0.0, child.overall_confidence) for child in children]
    if len(weights) != len(children):
        msg = (
            f"weights length ({len(weights)}) does not match "
            f"children length ({len(children)})"
        )
        raise ValueError(msg)
    return [max(0.0, w) for w in weights]


# ---- Per-dimension combine -------------------------------------------------


def _combine_dimension(
    per_child_entries: Sequence[tuple[WeightedDimension[_DimT], ...]],
    weights: Sequence[float],
    *,
    top_k: int,
) -> tuple[WeightedDimension[_DimT], ...]:
    """Combine one dimension's worth of children into a parent tuple.

    For each candidate value the parent's weight is
    ``sum_i(child_i_weight[v] x w_i) / sum_i(w_i)`` — a
    confidence-weighted (or weight-weighted) average of per-entry
    convictions. Children with zero effective weight contribute
    nothing (zero pass-through, anchoring the documented invariant).

    Args:
        per_child_entries: One tuple of weighted entries per child,
            aligned with ``weights``.
        weights: Per-child effective weight vector.
        top_k: Maximum entries on the result.

    Returns:
        A weight-descending tuple of weighted entries with at most
        ``top_k`` items; empty when no child contributed.
    """
    total = sum(weights)
    if total <= 0:
        return ()

    # First-seen ordering anchors the documented sort-stability
    # invariant: when two values tie on the combined weight, the one
    # whose first contributing child came earlier in the input lands
    # at the lower index. A plain dict preserves insertion order in
    # CPython 3.7+, which is the documented (and now mandated) language
    # behaviour.
    accumulator: dict[_DimT, float] = {}
    for entries, child_weight in zip(per_child_entries, weights, strict=True):
        if child_weight <= 0:
            continue
        for entry in entries:
            accumulator[entry.value] = (
                accumulator.get(entry.value, 0.0) + entry.weight * child_weight
            )

    combined: list[WeightedDimension[_DimT]] = []
    for value, summed in accumulator.items():
        weight = summed / total
        if weight < _DEFAULT_FLOOR:
            continue
        combined.append(WeightedDimension(value=value, weight=weight))

    # Sort by descending weight; the insertion-order preserved
    # accumulator iteration guarantees stable tie-breaking via
    # :py:meth:`list.sort`'s stability.
    combined.sort(key=lambda d: d.weight, reverse=True)
    return tuple(combined[:top_k])


# ---- Confidence combine ----------------------------------------------------


def _combine_confidence(
    children: Sequence[WeightedFragmentClassification],
    weights: Sequence[float],
    *,
    divergence_penalty: float,
) -> float:
    """Compute parent confidence from children's confidences + divergence.

    ``confidence_weighted_mean x (1 - divergence_penalty x jsd_norm)``,
    clamped to ``[0.0, 1.0]``.
    """
    total = sum(weights)
    if total <= 0:
        return 0.0

    confidence_weighted = sum(
        w * child.overall_confidence for w, child in zip(weights, children, strict=True)
    )
    mean_confidence = confidence_weighted / total

    if divergence_penalty <= 0.0:
        return _clamp(mean_confidence)

    jsd_norm = _mean_normalised_jsd(children, weights)
    # The penalty is scaled by ``mean_confidence`` so hedged children
    # disagreeing dampen less than confident children disagreeing
    # (the user's load-bearing intent on epic #364): two children at
    # confidence 0.3 pointing different ways are hedging — the
    # divergence is real but the parent is still entitled to its
    # already-low mean confidence. Two children at confidence 0.9
    # pointing different ways are an actual conflict — the parent
    # should drop further from the mean. Scaling by ``mean_confidence``
    # gives the dampening factor an interpretation in
    # ``[1 - divergence_penalty x mean_confidence², 1]``, which is
    # what the unit tests anchor under ``TestDivergenceDampening``.
    damping_factor = 1.0 - divergence_penalty * jsd_norm * mean_confidence
    return _clamp(mean_confidence * damping_factor)


def _mean_normalised_jsd(
    children: Sequence[WeightedFragmentClassification],
    weights: Sequence[float],
) -> float:
    """Mean normalised Jensen-Shannon divergence across dimensions.

    For each dimension we form one per-child probability distribution
    (per-entry conviction, normalised to sum to 1 within a child) and
    compute the weighted JSD across the contributing children's
    distributions, then average across dimensions. The result lives in
    ``[0.0, 1.0]``; ``0.0`` means perfect agreement, ``1.0`` means
    maximal disagreement.
    """
    dimension_accessors: tuple[
        tuple[str, type[StrEnum]],
        ...,
    ] = (
        ("frequencies", Frequency),
        ("phases", Phase),
        ("modes", Mode),
        ("orientations", Orientation),
        ("dosages", Dosage),
        ("voice_registers", VoiceRegister),
    )
    divergences: list[float] = []
    for attr, enum_type in dimension_accessors:
        per_child_entries = [getattr(child, attr) for child in children]
        jsd = _per_dimension_jsd(per_child_entries, weights, enum_type)
        if jsd is not None:
            divergences.append(jsd)
    if not divergences:
        return 0.0
    return sum(divergences) / len(divergences)


def _per_dimension_jsd(
    per_child_entries: Sequence[tuple[WeightedDimension[_DimT], ...]],
    weights: Sequence[float],
    enum_type: type[_DimT],
) -> float | None:
    """Compute the normalised JSD for one dimension, or ``None`` if undefined.

    Returns ``None`` when fewer than two children carry signal for
    this dimension (a single distribution has divergence zero by
    definition, but we want to skip dimensions where no comparison is
    possible rather than dilute the average with vacuous zeros).
    """
    # Build per-child distributions (sum-to-1 per child, zeros for
    # values the child did not mention) only for children with at
    # least one entry on this dimension and a positive effective
    # weight; the rest do not contribute to the JSD.
    distributions: list[dict[_DimT, float]] = []
    contributing_weights: list[float] = []
    for entries, weight in zip(per_child_entries, weights, strict=True):
        if weight <= 0 or not entries:
            continue
        total_entry_weight = sum(entry.weight for entry in entries)
        if total_entry_weight <= 0:
            continue
        dist = {entry.value: entry.weight / total_entry_weight for entry in entries}
        distributions.append(dist)
        contributing_weights.append(weight)

    if len(distributions) < 2:
        return None

    # All enum values that appear anywhere — the JSD is computed over
    # this support so each child's distribution is zero-padded
    # consistently. ``chain.from_iterable`` keeps the nested-iterable
    # flattening explicit and dodges the FURB179 readability flag.
    from itertools import (
        chain,  # locally scoped to keep the import cost off the module hot path
    )

    support = sorted(
        set(chain.from_iterable(distributions)),
        key=lambda v: list(enum_type).index(v),
    )
    total = sum(contributing_weights)
    mixture: dict[_DimT, float] = {
        value: sum(
            (w / total) * dist.get(value, 0.0)
            for w, dist in zip(contributing_weights, distributions, strict=True)
        )
        for value in support
    }

    jsd_raw = sum(
        (w / total) * _kl_divergence(distributions[i], mixture, support)
        for i, w in enumerate(contributing_weights)
    )
    # Normalise to [0, 1] using ``log(N)`` as the maximum-disagreement
    # ceiling for N distributions. Falls back to ``log(2)`` when only
    # two distributions are present so binary divergence still maps to
    # 1.0 at maximum disagreement.
    max_jsd = math.log(max(2, len(distributions)))
    if max_jsd <= 0:
        return 0.0
    return min(1.0, max(0.0, jsd_raw / max_jsd))


def _kl_divergence(
    distribution: dict[_DimT, float],
    mixture: dict[_DimT, float],
    support: Sequence[_DimT],
) -> float:
    """KL(P || M) over the documented support, treating 0*log(0) as 0."""
    accumulated = 0.0
    for value in support:
        p = distribution.get(value, 0.0)
        m = mixture.get(value, 0.0)
        if p <= 0.0 or m <= 0.0:
            continue
        accumulated += p * math.log(p / m)
    return accumulated


# ---- Reasoning assembly ----------------------------------------------------


def _compose_reasoning(
    children: Sequence[WeightedFragmentClassification],
    weights: Sequence[float],
) -> str:
    """Concatenate children's reasoning with traceable prefixes.

    Each child's reasoning is prefixed with
    ``[child {index} c={confidence:.2f}]`` so a reader can see at a
    glance which child pulled the parent which direction. The full
    result is truncated to :data:`_REASONING_BUDGET_CHARS` to bound
    the parent's reasoning size; truncation appends an ellipsis so
    the cut is visible.
    """
    if not children:
        return ""
    parts: list[str] = []
    for idx, (child, weight) in enumerate(zip(children, weights, strict=True)):
        if weight <= 0 or not child.reasoning:
            continue
        prefix = f"[child {idx} c={child.overall_confidence:.2f}]"
        parts.append(f"{prefix} {child.reasoning}")
    combined = "\n".join(parts)
    if len(combined) <= _REASONING_BUDGET_CHARS:
        return combined
    return combined[: _REASONING_BUDGET_CHARS - 1].rstrip() + "…"


def _clamp(value: float, *, low: float = 0.0, high: float = 1.0) -> float:
    """Clamp ``value`` into ``[low, high]``; non-finite values fall to ``low``."""
    if not math.isfinite(value):
        return low
    if value < low:
        return low
    if value > high:
        return high
    return value
