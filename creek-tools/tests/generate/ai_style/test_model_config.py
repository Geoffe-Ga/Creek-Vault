"""Tests for the AI-style model types and config (FEAT-040.1)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from creek.config import AIStyleConfig, CreekConfig
from creek.generate.ai_style.model import FeatureStat, VoiceFingerprint


class TestVoiceFingerprint:
    """Lookup and thin-corpus helpers."""

    def test_rate_and_support_for_present_feature(self) -> None:
        """A measured feature returns its rate and support."""
        fp = VoiceFingerprint(
            features={"em_dash_density": FeatureStat(rate=3.5, support=12)},
            fragment_count=12,
        )
        assert fp.rate_for("em_dash_density") == 3.5
        assert fp.support_for("em_dash_density") == 12

    def test_absent_feature_returns_none_and_zero(self) -> None:
        """An unmeasured feature is the sparse-corpus fallback signal."""
        fp = VoiceFingerprint(features={}, fragment_count=0)
        assert fp.rate_for("missing") is None
        assert fp.support_for("missing") == 0

    def test_is_thin_threshold(self) -> None:
        """Fewer than the minimum fragments marks the fingerprint thin."""
        assert VoiceFingerprint(fragment_count=4).is_thin(5) is True
        assert VoiceFingerprint(fragment_count=5).is_thin(5) is False


class TestAIStyleConfig:
    """Weight resolution and CreekConfig wiring."""

    def test_feature_override_wins(self) -> None:
        """A per-feature weight overrides the category weight."""
        config = AIStyleConfig(
            category_weights={"lexical": 1.0},
            feature_weights={"ai_vocab.tapestry": 4.0},
        )
        assert (
            config.weight_for(category="lexical", feature_key="ai_vocab.tapestry")
            == 4.0
        )

    def test_category_weight_fallback(self) -> None:
        """Without an override the feature inherits its category weight."""
        config = AIStyleConfig(category_weights={"mechanical": 0.5})
        assert config.weight_for(category="mechanical", feature_key="anything") == 0.5

    def test_unlisted_category_defaults_to_one(self) -> None:
        """An unlisted category falls back to weight 1.0."""
        config = AIStyleConfig(category_weights={})
        assert config.weight_for(category="ghost", feature_key="x") == 1.0

    def test_creek_config_exposes_ai_style(self) -> None:
        """CreekConfig carries an ai_style section with sane defaults."""
        config = CreekConfig()
        assert config.ai_style.enabled is True
        assert config.ai_style.voice_distance_upper > 0.0
        assert "mechanical" in config.ai_style.enabled_categories

    def test_ai_style_round_trips_through_dump(self) -> None:
        """The section serialises (used by generate_default_config)."""
        data = CreekConfig().model_dump(mode="json")
        assert "ai_style" in data
        assert data["ai_style"]["enabled"] is True

    def test_prompt_prevention_defaults(self) -> None:
        """FEAT-040.8 prevention is on by default with a sane length cap."""
        config = AIStyleConfig()
        assert config.prevent_in_prompt is True
        assert config.include_measured_targets is True
        assert config.preamble_max_chars == 1200

    def test_preamble_max_chars_rejects_negative(self) -> None:
        """The length cap is constrained to be non-negative."""
        with pytest.raises(ValidationError):
            AIStyleConfig(preamble_max_chars=-1)


class TestAIStyleWeightsRejectNonFiniteAndNegative:
    """Every AIStyleConfig weight must be finite and non-negative (#1615).

    These maps feed the voice fingerprint's weighted aggregation. The
    three bad values fail in three different ways, and none is detectable
    downstream:

    * ``+inf`` **poisons** — every feature ``rate`` becomes NaN and
      ``save_fingerprint`` writes a literal ``NaN`` token into
      ``voice-fingerprint.json``, which is not valid JSON for strict
      parsers.
    * ``NaN`` and **negative** weights are silently *dropped* by
      ``fingerprint.py``'s ``if weight > 0.0`` gate, shrinking the corpus
      with no warning (measured: ``fragment_count`` 2 → 1).

    Mirrors the #1412 validator on the authority maps.
    """

    @pytest.mark.parametrize(
        "bad",
        [
            pytest.param(float("nan"), id="nan"),
            pytest.param(float("inf"), id="positive-infinity"),
            pytest.param(float("-inf"), id="negative-infinity"),
            pytest.param(-1.0, id="negative"),
        ],
    )
    @pytest.mark.parametrize(
        "field",
        [
            pytest.param("authorship_weights", id="authorship_weights"),
            pytest.param("feature_weights", id="feature_weights"),
        ],
    )
    def test_weight_maps_reject_bad_values(self, field: str, bad: float) -> None:
        """Each str-keyed weight map refuses NaN, infinities and negatives.

        Args:
            field: The weight map under test.
            bad: The value that must be refused.
        """
        with pytest.raises(ValidationError):
            AIStyleConfig(**{field: {"journal": bad}})

    @pytest.mark.parametrize(
        "bad",
        [
            pytest.param(float("nan"), id="nan"),
            pytest.param(float("inf"), id="positive-infinity"),
            pytest.param(float("-inf"), id="negative-infinity"),
            pytest.param(-1.0, id="negative"),
        ],
    )
    def test_authorship_default_weight_rejects_bad_values(self, bad: float) -> None:
        """The scalar fallback is gated too.

        Its ``ge=0.0`` bound admits ``+inf`` — a range check rejects NaN
        only as an accident of ``<`` always being ``False``, and does not
        reject infinity at all.

        Args:
            bad: The value that must be refused.
        """
        with pytest.raises(ValidationError):
            AIStyleConfig(authorship_default_weight=bad)

    @pytest.mark.parametrize(
        "bad",
        [
            pytest.param(float("nan"), id="nan"),
            pytest.param(float("inf"), id="positive-infinity"),
            pytest.param(-1.0, id="negative"),
        ],
    )
    def test_category_weights_reject_bad_values(self, bad: float) -> None:
        """The enum-keyed sibling map has the identical gap.

        Kept separate from the str-keyed maps because its keys are
        ``AIStyleCategory`` literals, not free strings.

        Args:
            bad: The value that must be refused.
        """
        with pytest.raises(ValidationError):
            AIStyleConfig(category_weights={"mechanical": bad})

    def test_zero_stays_legal(self) -> None:
        """Zero means "contributes nothing", which is a real setting."""
        config = AIStyleConfig(authorship_weights={"journal": 0.0})
        assert config.authorship_weights["journal"] == 0.0

    def test_the_error_names_the_offending_key(self) -> None:
        """A config error must be actionable without bisecting the YAML."""
        with pytest.raises(ValidationError, match="chatgpt"):
            AIStyleConfig(authorship_weights={"journal": 1.0, "chatgpt": float("nan")})
