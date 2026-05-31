"""Tests for the AI-style model types and config (FEAT-040.1)."""

from __future__ import annotations

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
