"""Tests for the voice-distance scoring model (FEAT-040.1)."""

from __future__ import annotations

import pytest

from creek.generate.ai_style.distance import (
    FeatureContribution,
    bad_direction_magnitude,
    voice_distance,
)


class TestBadDirectionMagnitude:
    """The concerning-direction divergence for each polarity."""

    def test_avoid_over_use_is_positive(self) -> None:
        """An avoided feature used more than the user yields the gap."""
        assert bad_direction_magnitude("avoid", draft_rate=3.0, user_rate=1.0) == 2.0

    def test_avoid_under_use_is_zero(self) -> None:
        """Using an avoided feature less than the user is harmless."""
        assert bad_direction_magnitude("avoid", draft_rate=0.5, user_rate=2.0) == 0.0

    def test_signature_under_use_is_positive(self) -> None:
        """A signature feature used less than the user yields the gap."""
        assert (
            bad_direction_magnitude("signature", draft_rate=1.0, user_rate=4.0) == 3.0
        )

    def test_signature_over_use_is_zero(self) -> None:
        """Using a signature feature more than the user is harmless."""
        assert (
            bad_direction_magnitude("signature", draft_rate=5.0, user_rate=2.0) == 0.0
        )

    def test_equal_rates_are_zero(self) -> None:
        """Matching the user exactly contributes nothing, either polarity."""
        assert bad_direction_magnitude("avoid", draft_rate=2.0, user_rate=2.0) == 0.0
        assert (
            bad_direction_magnitude("signature", draft_rate=2.0, user_rate=2.0) == 0.0
        )


class TestVoiceDistance:
    """The aggregate, bounded, weighted voice distance."""

    def test_no_contributions_is_zero(self) -> None:
        """An empty contribution list scores zero, not a divide-by-zero."""
        assert voice_distance([]) == 0.0

    def test_zero_magnitudes_score_zero(self) -> None:
        """Matching the user on every feature yields distance 0."""
        items = [
            FeatureContribution("a", weight=1.0, magnitude=0.0),
            FeatureContribution("b", weight=2.0, magnitude=0.0),
        ]
        assert voice_distance(items) == 0.0

    def test_distance_rises_with_divergence(self) -> None:
        """Larger divergence ⇒ larger distance."""
        small = voice_distance([FeatureContribution("a", 1.0, 1.0)])
        large = voice_distance([FeatureContribution("a", 1.0, 50.0)])
        assert 0.0 < small < large < 1.0

    def test_distance_is_bounded_below_one(self) -> None:
        """Even an enormous divergence saturates below 1.0."""
        assert voice_distance([FeatureContribution("a", 1.0, 1e6)]) < 1.0

    def test_zero_total_weight_scores_zero(self) -> None:
        """A zero-weight feature cannot produce a non-zero distance."""
        assert voice_distance([FeatureContribution("a", 0.0, 99.0)]) == 0.0

    def test_softening_scales_the_score_down(self) -> None:
        """Thin-fingerprint softening reduces the distance proportionally."""
        items = [FeatureContribution("a", 1.0, 10.0)]
        full = voice_distance(items)
        softened = voice_distance(items, softening=0.25)
        assert softened == pytest.approx(full * 0.25)

    def test_weight_biases_the_mean(self) -> None:
        """A heavier feature pulls the weighted mean toward its magnitude."""
        balanced = voice_distance(
            [
                FeatureContribution("a", 1.0, 0.0),
                FeatureContribution("b", 1.0, 50.0),
            ],
        )
        b_heavy = voice_distance(
            [
                FeatureContribution("a", 1.0, 0.0),
                FeatureContribution("b", 9.0, 50.0),
            ],
        )
        assert b_heavy > balanced
