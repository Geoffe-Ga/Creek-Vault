"""Tests for the holonic combine / decompose math (issue #367).

Covers :mod:`creek.classify.holonic` — the pure-math operators at the
heart of epic #364. The tests exercise every invariant the module
docstring documents (idempotence, monotonicity, sort stability,
divergence dampening, zero-confidence pass-through, conviction-over-
length) plus a handful of realistic mini-cases that match the
integration tests landing in #368 / #369.

Conviction x confidence is the contract: a child's influence on its
parent defaults to its own ``overall_confidence``, and the parent's
per-value weight is a confidence-weighted average of per-entry
convictions. Length is not the default mass; the
``test_conviction_over_length_load_bearing`` test anchors that
property explicitly.
"""

from __future__ import annotations

import pytest

from creek.classify.holonic import combine, decompose_prior
from creek.classify.weighted import (
    WeightedDimension,
    WeightedFragmentClassification,
)
from creek.models import (
    Confidence,
    Dosage,
    Frequency,
    Mode,
    Orientation,
    Phase,
    VoiceRegister,
)

# ---- Fixture helpers -------------------------------------------------------


def _wfc(
    *,
    frequencies: tuple[tuple[Frequency, float], ...] = (),
    phases: tuple[tuple[Phase, float], ...] = (),
    modes: tuple[tuple[Mode, float], ...] = (),
    orientations: tuple[tuple[Orientation, float], ...] = (),
    dosages: tuple[tuple[Dosage, float], ...] = (),
    voice_registers: tuple[tuple[VoiceRegister, float], ...] = (),
    confidences: tuple[tuple[Confidence, float], ...] = (),
    overall_confidence: float = 0.7,
    reasoning: str = "",
) -> WeightedFragmentClassification:
    """Build a :class:`WeightedFragmentClassification` from concise tuples.

    The dimension arguments accept ``(value, weight)`` pairs which the
    helper lifts into :class:`WeightedDimension` instances. Keeps the
    test fixtures readable without an explicit
    ``WeightedDimension(value=..., weight=...)`` everywhere.
    """
    return WeightedFragmentClassification(
        frequencies=tuple(WeightedDimension(value=v, weight=w) for v, w in frequencies),
        phases=tuple(WeightedDimension(value=v, weight=w) for v, w in phases),
        modes=tuple(WeightedDimension(value=v, weight=w) for v, w in modes),
        orientations=tuple(
            WeightedDimension(value=v, weight=w) for v, w in orientations
        ),
        dosages=tuple(WeightedDimension(value=v, weight=w) for v, w in dosages),
        voice_registers=tuple(
            WeightedDimension(value=v, weight=w) for v, w in voice_registers
        ),
        confidences=tuple(WeightedDimension(value=v, weight=w) for v, w in confidences),
        overall_confidence=overall_confidence,
        reasoning=reasoning,
    )


def _freq_weight(parent: WeightedFragmentClassification, value: Frequency) -> float:
    """Return the parent's per-value weight on ``value`` (0.0 if absent)."""
    for entry in parent.frequencies:
        if entry.value is value:
            return entry.weight
    return 0.0


# ---- Edge cases ------------------------------------------------------------


class TestEdgeCases:
    """Empty / pathological inputs return honest, well-formed defaults."""

    def test_empty_children_returns_empty(self) -> None:
        """No children → empty :class:`WeightedFragmentClassification`."""
        assert combine([]) == WeightedFragmentClassification()

    def test_all_zero_weights_returns_empty_dimensions(self) -> None:
        """When all child weights resolve to zero the parent has no signal.

        Reasoning still rolls up because the empty-children case
        already documents the contract on the dataclass; the
        per-dimension tuples are empty and confidence is zero.
        """
        child = _wfc(
            frequencies=((Frequency.F3, 0.9),),
            overall_confidence=0.0,
        )
        parent = combine([child, child])
        assert parent.frequencies == ()
        assert parent.overall_confidence == 0.0

    def test_mismatched_explicit_weights_raises(self) -> None:
        """A wrong-length ``weights`` is a caller bug; raise ``ValueError``."""
        child = _wfc()
        with pytest.raises(ValueError, match="weights length"):
            combine([child], weights=[1.0, 1.0])

    def test_negative_explicit_weight_clamped_to_zero(self) -> None:
        """A negative explicit override is clamped, not inverted."""
        child_a = _wfc(frequencies=((Frequency.F3, 0.9),))
        child_b = _wfc(frequencies=((Frequency.F5, 0.9),))
        parent = combine([child_a, child_b], weights=[1.0, -1.0])
        # Negative weight → child_b contributes nothing; parent
        # mirrors child_a (idempotence on N=1).
        assert parent.frequencies == (
            WeightedDimension(value=Frequency.F3, weight=0.9),
        )


# ---- Idempotence and scale invariance --------------------------------------


class TestIdempotence:
    """``combine`` of a single (or identical) child reproduces the child."""

    def test_idempotence_on_single_child(self) -> None:
        """``combine([c]) == c`` modulo reasoning prefix."""
        child = _wfc(
            frequencies=((Frequency.F3, 0.8), (Frequency.F5, 0.4)),
            phases=((Phase.RISING, 0.7),),
            overall_confidence=0.85,
            reasoning="a clear story",
        )
        parent = combine([child])
        assert parent.frequencies == child.frequencies
        assert parent.phases == child.phases
        assert parent.overall_confidence == pytest.approx(child.overall_confidence)

    def test_idempotence_on_identical_children(self) -> None:
        """``combine([c] * n) == c`` per the documented invariant."""
        child = _wfc(
            frequencies=((Frequency.F3, 0.7),),
            phases=((Phase.RISING, 0.6),),
            overall_confidence=0.8,
        )
        parent = combine([child, child, child])
        # Use ``pytest.approx`` because floating-point summation of
        # identical weights drifts off the exact source value
        # (0.7 + 0.7 + 0.7) / 3 ≠ 0.7 in IEEE754.
        assert len(parent.frequencies) == 1
        assert parent.frequencies[0].value is Frequency.F3
        assert parent.frequencies[0].weight == pytest.approx(0.7)
        assert len(parent.phases) == 1
        assert parent.phases[0].value is Phase.RISING
        assert parent.phases[0].weight == pytest.approx(0.6)
        assert parent.overall_confidence == pytest.approx(child.overall_confidence)

    def test_confidence_uniform_scaling_invariance(self) -> None:
        """Scaling every child's effective weight by ``k`` is a no-op."""
        children = [
            _wfc(frequencies=((Frequency.F3, 0.8),), overall_confidence=0.4),
            _wfc(frequencies=((Frequency.F5, 0.6),), overall_confidence=0.4),
        ]
        weights_a = [0.4, 0.4]
        weights_b = [0.8, 0.8]
        parent_a = combine(children, weights=weights_a)
        parent_b = combine(children, weights=weights_b)
        assert parent_a.frequencies == parent_b.frequencies
        assert parent_a.overall_confidence == pytest.approx(parent_b.overall_confidence)


# ---- Zero-confidence pass-through ------------------------------------------


class TestZeroConfidencePassThrough:
    """A child with ``overall_confidence=0`` contributes nothing."""

    def test_zero_confidence_child_omitted(self) -> None:
        """``combine([c, zero])`` matches ``combine([c])`` on per-dim weights."""
        confident = _wfc(
            frequencies=((Frequency.F3, 0.9),),
            overall_confidence=0.9,
        )
        zero = _wfc(
            frequencies=((Frequency.F5, 1.0),),
            overall_confidence=0.0,
        )
        parent = combine([confident, zero])
        assert parent.frequencies == (
            WeightedDimension(value=Frequency.F3, weight=0.9),
        )

    def test_zero_confidence_child_does_not_pollute_confidence(self) -> None:
        """Zero-confidence siblings do not drag down the confident parent."""
        confident = _wfc(
            frequencies=((Frequency.F3, 0.9),),
            overall_confidence=0.9,
        )
        zero = _wfc(overall_confidence=0.0)
        parent = combine([confident, zero])
        # The zero-weight child contributes neither to weights nor to
        # the confidence-weighted mean (its weight is its own zero
        # confidence). The parent's confidence equals the lone
        # contributor's.
        assert parent.overall_confidence == pytest.approx(0.9)


# ---- The user's load-bearing case ------------------------------------------


class TestConvictionOverLength:
    """A confident-short atom outweighs many hedged-long atoms."""

    def test_one_confident_atom_beats_four_hedged_ones(self) -> None:
        """The user's example: ``{F3: 0.9 c=0.9}`` beats ``{F5: 0.3 c=0.2}`` x 4."""
        confident = _wfc(
            frequencies=((Frequency.F3, 0.9),),
            overall_confidence=0.9,
        )
        hedged = _wfc(
            frequencies=((Frequency.F5, 0.3),),
            overall_confidence=0.2,
        )
        parent = combine([confident, hedged, hedged, hedged, hedged])
        # Parent's top frequency is F3 (not F5) — load-bearing assertion.
        assert parent.frequencies[0].value is Frequency.F3
        # And F3 still dominates by a clear margin.
        assert _freq_weight(parent, Frequency.F3) > _freq_weight(parent, Frequency.F5)


# ---- Monotonicity ----------------------------------------------------------


class TestMonotonicity:
    """Increasing a child's mass shifts the parent toward it monotonically."""

    def test_monotonicity_in_explicit_weight(self) -> None:
        """As one child's weight rises, its per-value pull on the parent grows."""
        f3_child = _wfc(frequencies=((Frequency.F3, 0.9),), overall_confidence=0.8)
        f5_child = _wfc(frequencies=((Frequency.F5, 0.9),), overall_confidence=0.8)
        f3_at_low = _freq_weight(
            combine([f3_child, f5_child], weights=[0.2, 0.8]), Frequency.F3
        )
        f3_at_mid = _freq_weight(
            combine([f3_child, f5_child], weights=[0.5, 0.5]), Frequency.F3
        )
        f3_at_high = _freq_weight(
            combine([f3_child, f5_child], weights=[0.8, 0.2]), Frequency.F3
        )
        assert f3_at_low < f3_at_mid < f3_at_high

    def test_monotonicity_in_confidence(self) -> None:
        """As one child's overall_confidence rises, its influence grows."""
        f3_low = _wfc(
            frequencies=((Frequency.F3, 0.9),),
            overall_confidence=0.2,
        )
        f3_high = _wfc(
            frequencies=((Frequency.F3, 0.9),),
            overall_confidence=0.9,
        )
        f5_fixed = _wfc(
            frequencies=((Frequency.F5, 0.9),),
            overall_confidence=0.5,
        )
        weight_low = _freq_weight(combine([f3_low, f5_fixed]), Frequency.F3)
        weight_high = _freq_weight(combine([f3_high, f5_fixed]), Frequency.F3)
        assert weight_low < weight_high


# ---- Normalisation ---------------------------------------------------------


class TestNormalisation:
    """Per-dimension weights stay in ``[0, 1]`` and respect ``top_k``."""

    def test_per_value_weights_in_unit_interval(self) -> None:
        """No combined weight exceeds 1.0 even with strongly-agreeing children."""
        child = _wfc(frequencies=((Frequency.F3, 1.0),), overall_confidence=1.0)
        parent = combine([child, child, child])
        for entry in parent.frequencies:
            assert 0.0 <= entry.weight <= 1.0

    def test_top_k_cap(self) -> None:
        """The parent's per-dimension tuple is capped at ``top_k`` entries."""
        # Construct a child mentioning many frequencies; the cap should
        # surface only the top three when ``top_k=3``.
        child = _wfc(
            frequencies=(
                (Frequency.F1, 0.9),
                (Frequency.F2, 0.85),
                (Frequency.F3, 0.8),
                (Frequency.F4, 0.75),
                (Frequency.F5, 0.7),
                (Frequency.F6, 0.65),
            ),
            overall_confidence=0.7,
        )
        parent = combine([child], top_k=3)
        assert len(parent.frequencies) == 3
        # Top-3 are the highest-conviction values from the child.
        assert [entry.value for entry in parent.frequencies] == [
            Frequency.F1,
            Frequency.F2,
            Frequency.F3,
        ]

    def test_floor_drops_low_weight_entries(self) -> None:
        """Combined weights below 0.05 are dropped from the parent tuple."""
        # Two children, one mentions F3 strongly, another mentions F5
        # at a tiny weight that, after averaging, falls below the floor.
        confident = _wfc(
            frequencies=((Frequency.F3, 0.9),),
            overall_confidence=0.9,
        )
        whisper = _wfc(
            frequencies=((Frequency.F5, 0.04),),
            overall_confidence=0.9,
        )
        parent = combine([confident, whisper])
        assert all(entry.value is not Frequency.F5 for entry in parent.frequencies)

    def test_overall_confidence_clamped(self) -> None:
        """Parent confidence stays in ``[0, 1]`` even with extreme inputs."""
        child = _wfc(overall_confidence=1.0)
        parent = combine([child, child])
        assert 0.0 <= parent.overall_confidence <= 1.0


# ---- Divergence dampening --------------------------------------------------


class TestDivergenceDampening:
    """Disagreement among confident children dampens parent confidence."""

    def test_agreement_preserves_confidence(self) -> None:
        """All-identical children → no dampening; parent matches mean."""
        child = _wfc(
            frequencies=((Frequency.F3, 0.8),),
            phases=((Phase.RISING, 0.7),),
            overall_confidence=0.9,
        )
        parent = combine([child, child, child])
        assert parent.overall_confidence == pytest.approx(0.9)

    def test_confident_disagreement_dampens_confidence(self) -> None:
        """Confident children pointing different ways drop parent confidence."""
        f3 = _wfc(
            frequencies=((Frequency.F3, 0.9),),
            phases=((Phase.RISING, 0.9),),
            overall_confidence=0.9,
        )
        f5 = _wfc(
            frequencies=((Frequency.F5, 0.9),),
            phases=((Phase.WITHDRAWAL, 0.9),),
            overall_confidence=0.9,
        )
        parent = combine([f3, f5])
        # Confidence is strictly less than the agreeing-case mean of 0.9.
        assert parent.overall_confidence < 0.9

    def test_hedged_disagreement_dampens_less(self) -> None:
        """Hedged children disagreeing dampen less than confident ones."""
        hedged_a = _wfc(
            frequencies=((Frequency.F3, 0.3),),
            overall_confidence=0.3,
        )
        hedged_b = _wfc(
            frequencies=((Frequency.F5, 0.3),),
            overall_confidence=0.3,
        )
        confident_a = _wfc(
            frequencies=((Frequency.F3, 0.9),),
            overall_confidence=0.9,
        )
        confident_b = _wfc(
            frequencies=((Frequency.F5, 0.9),),
            overall_confidence=0.9,
        )
        hedged_parent = combine([hedged_a, hedged_b])
        confident_parent = combine([confident_a, confident_b])
        # Confident disagreement loses more relative confidence than hedged.
        # Mean confidences match perfectly (0.3 vs 0.9 weighted-mean equals
        # themselves with two equal-confidence children), so the ratio of
        # actual to mean is the comparison surface.
        hedged_ratio = hedged_parent.overall_confidence / 0.3
        confident_ratio = confident_parent.overall_confidence / 0.9
        assert confident_ratio < hedged_ratio

    def test_zero_penalty_disables_dampening(self) -> None:
        """``divergence_penalty=0.0`` collapses to the weighted-mean confidence."""
        f3 = _wfc(
            frequencies=((Frequency.F3, 0.9),),
            overall_confidence=0.5,
        )
        f5 = _wfc(
            frequencies=((Frequency.F5, 0.9),),
            overall_confidence=0.5,
        )
        parent = combine([f3, f5], divergence_penalty=0.0)
        assert parent.overall_confidence == pytest.approx(0.5)


# ---- Sort stability --------------------------------------------------------


class TestSortStability:
    """Ties in combined weight preserve input order."""

    def test_tie_preserves_first_seen_order(self) -> None:
        """Two values tied on combined weight respect input order."""
        # Two children, each mentioning a different value at identical
        # weight x confidence. The combined tuple should preserve the
        # first-seen order (F3 from child_a, then F5 from child_b).
        child_a = _wfc(frequencies=((Frequency.F3, 0.5),), overall_confidence=0.7)
        child_b = _wfc(frequencies=((Frequency.F5, 0.5),), overall_confidence=0.7)
        parent = combine([child_a, child_b])
        assert [entry.value for entry in parent.frequencies] == [
            Frequency.F3,
            Frequency.F5,
        ]


# ---- Realistic mini-cases --------------------------------------------------


class TestRealisticMiniCases:
    """End-to-end examples that match the eventual integration tests."""

    def test_three_atoms_f3_dominant_paragraph(self) -> None:
        """Three F3-leaning atoms produce an F3-dominant parent."""
        a = _wfc(
            frequencies=((Frequency.F3, 0.8),),
            overall_confidence=0.9,
            reasoning="a",
        )
        b = _wfc(
            frequencies=((Frequency.F3, 0.6), (Frequency.F5, 0.4)),
            overall_confidence=0.7,
            reasoning="b",
        )
        c = _wfc(
            frequencies=((Frequency.F3, 0.7),),
            overall_confidence=0.85,
            reasoning="c",
        )
        parent = combine([a, b, c])
        assert parent.frequencies[0].value is Frequency.F3
        # Parent overall_confidence is bounded by the confidence-weighted
        # mean, dampened a touch by the F5 sliver in child b.
        assert parent.overall_confidence <= max(
            a.overall_confidence,
            b.overall_confidence,
            c.overall_confidence,
        )

    def test_three_atoms_spread_dampens_confidence(self) -> None:
        """Three confident atoms spread across F3/F5/F7 dampen confidence."""
        a = _wfc(
            frequencies=((Frequency.F3, 0.9),),
            overall_confidence=0.9,
        )
        b = _wfc(
            frequencies=((Frequency.F5, 0.9),),
            overall_confidence=0.9,
        )
        c = _wfc(
            frequencies=((Frequency.F7, 0.9),),
            overall_confidence=0.9,
        )
        parent = combine([a, b, c])
        # Confidence is strictly less than the mean of 0.9.
        assert parent.overall_confidence < 0.9
        # And all three frequencies surface.
        surfaced = {entry.value for entry in parent.frequencies}
        assert Frequency.F3 in surfaced
        assert Frequency.F5 in surfaced
        assert Frequency.F7 in surfaced


# ---- Reasoning composition -------------------------------------------------


class TestReasoning:
    """The parent's reasoning carries traceable per-child prefixes."""

    def test_reasoning_prefixed_with_child_index(self) -> None:
        """Each child's reasoning gets prefixed with its index and confidence."""
        a = _wfc(overall_confidence=0.7, reasoning="alpha")
        b = _wfc(overall_confidence=0.5, reasoning="beta")
        parent = combine([a, b])
        assert "[child 0 c=0.70] alpha" in parent.reasoning
        assert "[child 1 c=0.50] beta" in parent.reasoning

    def test_truncation_appends_ellipsis(self) -> None:
        """Reasoning longer than the budget is truncated with a trailing ellipsis."""
        # Each child gives 3000 characters; with two children + prefixes
        # the budget (4000) is exceeded and the result truncates.
        long_text = "x" * 3000
        a = _wfc(overall_confidence=0.7, reasoning=long_text)
        b = _wfc(overall_confidence=0.5, reasoning=long_text)
        parent = combine([a, b])
        assert parent.reasoning.endswith("…")
        # Budget is enforced (the trailing ellipsis sits at the
        # documented size minus one).
        assert len(parent.reasoning) <= 4000


# ---- decompose_prior -------------------------------------------------------


class TestDecomposePrior:
    """The dual operator produces a soft prior atoms can override."""

    def test_dimensions_mirror_parent(self) -> None:
        """Every per-dimension tuple flows through unchanged."""
        parent = _wfc(
            frequencies=((Frequency.F3, 0.8),),
            phases=((Phase.RISING, 0.7),),
            overall_confidence=0.9,
        )
        prior = decompose_prior(parent, n_atoms=3)
        assert prior.frequencies == parent.frequencies
        assert prior.phases == parent.phases

    def test_confidence_is_halved(self) -> None:
        """The prior's confidence is half the parent's so atoms can override."""
        parent = _wfc(overall_confidence=0.8)
        prior = decompose_prior(parent, n_atoms=2)
        assert prior.overall_confidence == pytest.approx(0.4)

    def test_unused_n_atoms_does_not_error(self) -> None:
        """``n_atoms`` is accepted for forward compatibility (no current effect)."""
        parent = _wfc(overall_confidence=0.5)
        # Different values produce identical output today; this anchors
        # the documented forward-compat behaviour.
        for n in (1, 2, 100):
            assert decompose_prior(
                parent, n_atoms=n
            ).overall_confidence == pytest.approx(0.25)


# ---- Defensive-branch edge cases -------------------------------------------


class TestDefensiveBranches:
    """Defensive code paths covering pathological inputs."""

    def test_nan_confidence_collapses_to_zero(self) -> None:
        """A NaN per-child confidence does not propagate to the parent."""
        import math

        child = _wfc(overall_confidence=float("nan"))
        parent = combine([child, child])
        # ``max(0.0, nan) == 0.0`` in CPython (nan > 0.0 is False), so
        # ``_resolve_weights`` silences the NaN at the door: the
        # effective weight becomes 0.0, the sum is 0.0, and the
        # all-zero-weights short-circuit returns a zero-confidence
        # parent. The clamp is therefore never reached on this path.
        assert math.isfinite(parent.overall_confidence)
        assert parent.overall_confidence == 0.0

    def test_explicit_zero_weights_returns_empty_dimensions(self) -> None:
        """All-zero explicit weights collapse the parent to no signal."""
        # ``_combine_dimension``'s ``total <= 0`` short-circuit is
        # reached via an explicit zero ``weights`` override; the
        # children themselves still carry per-entry conviction.
        child = _wfc(
            frequencies=((Frequency.F3, 0.9),),
            overall_confidence=0.9,
        )
        parent = combine([child, child], weights=[0.0, 0.0])
        assert parent.frequencies == ()
        assert parent.overall_confidence == 0.0

    def test_lone_contributor_skips_jsd(self) -> None:
        """A dimension with only one contributing distribution skips dampening."""
        with_signal = _wfc(
            frequencies=((Frequency.F3, 0.8),),
            overall_confidence=0.7,
        )
        without_signal = _wfc(overall_confidence=0.7)
        parent = combine([with_signal, without_signal])
        # No dimension has ≥2 contributing distributions, so JSD is
        # skipped and parent confidence equals the mean.
        assert parent.frequencies[0].value is Frequency.F3
        assert parent.overall_confidence == pytest.approx(0.7)


class TestConfidencesDimension:
    """The author-stance axis is threaded through every holonic path (#1309)."""

    def test_combine_rolls_up_children_confidences(self) -> None:
        """A bubbled-up parent carries the children's author stance."""
        agreeing = _wfc(
            confidences=((Confidence.CONVICTION, 0.9),),
            overall_confidence=0.8,
        )
        parent = combine([agreeing, agreeing])
        assert parent.confidences != ()
        assert parent.confidences[0].value is Confidence.CONVICTION

    def test_decompose_prior_carries_confidences_down(self) -> None:
        """A decomposed prior hands the parent's stance to its children."""
        parent = _wfc(
            confidences=((Confidence.SETTLED, 0.8),),
            overall_confidence=0.8,
        )
        assert decompose_prior(parent, n_atoms=3).confidences == parent.confidences

    def test_opposed_confidences_dampen_parent_confidence(self) -> None:
        """Disagreement about author stance contributes to the JSD penalty.

        The regression guard for a hole that nothing else catches.
        ``_mean_normalised_jsd`` walks a hard-coded ``dimension_accessors``
        table; ``confidences`` has to be listed there explicitly. If it is
        omitted, ``combine`` and ``decompose_prior`` still thread the axis, so
        every other test in this class stays green — but two children that
        disagree maximally about how firmly the writer holds their claim
        contribute ZERO divergence, and the parent's ``overall_confidence`` is
        silently overstated.

        Verified to be a real binding: deleting the ``("confidences",
        Confidence)`` accessor turns this test, and only this test, red.
        """
        musing = _wfc(
            confidences=((Confidence.MUSING, 0.9),),
            overall_confidence=0.8,
        )
        conviction = _wfc(
            confidences=((Confidence.CONVICTION, 0.9),),
            overall_confidence=0.8,
        )
        opposed = combine([musing, conviction])
        agreed = combine([musing, musing])

        # Same per-child confidence in both cases, so any difference is
        # attributable to the divergence penalty alone.
        assert agreed.overall_confidence == pytest.approx(0.8)
        assert opposed.overall_confidence < agreed.overall_confidence
