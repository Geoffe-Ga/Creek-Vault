"""Tests for the PrivacyClassifier (issue #27)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from creek.classify.privacy import RECOVERY_KEYWORDS, PrivacyClassifier
from creek.classify.review import ReviewQueueGenerator
from creek.models import (
    Confidence,
    Fragment,
    FragmentSource,
    Frequency,
    FrequencyClassification,
    PrivacyTier,
    SourcePlatform,
    VoiceClassification,
    VoiceRegister,
)


def _make_fragment(
    *,
    title: str = "A note",
    platform: SourcePlatform = SourcePlatform.OTHER,
    channel: str | None = None,
    register: VoiceRegister | None = None,
    confidence: Confidence | None = None,
) -> Fragment:
    """Build a deterministic Fragment for tests."""
    return Fragment(
        title=title,
        source=FragmentSource(platform=platform, channel=channel),
        created=datetime(2026, 4, 1, tzinfo=UTC),
        ingested=datetime(2026, 4, 1, tzinfo=UTC),
        voice=VoiceClassification(
            voice_register=register,
            confidence=confidence,
        ),
    )


# ---- classify_tier ----------------------------------------------------


class TestClassifyTier:
    """``classify_tier`` assigns the right privacy bucket from source/content."""

    def test_essay_source_is_public(self) -> None:
        """Essays are published outputs and get the PUBLIC tier."""
        classifier = PrivacyClassifier()
        frag = _make_fragment(platform=SourcePlatform.ESSAY)
        assert classifier.classify_tier(frag) == PrivacyTier.PUBLIC

    def test_journal_source_is_intimate(self) -> None:
        """Journal entries are always treated as INTIMATE."""
        classifier = PrivacyClassifier()
        frag = _make_fragment(platform=SourcePlatform.JOURNAL)
        assert classifier.classify_tier(frag) == PrivacyTier.INTIMATE

    def test_chatbot_source_is_personal(self) -> None:
        """Chatbot conversations are personal by default."""
        classifier = PrivacyClassifier()
        for platform in (SourcePlatform.CLAUDE, SourcePlatform.CHATGPT):
            frag = _make_fragment(platform=platform)
            assert classifier.classify_tier(frag) == PrivacyTier.PERSONAL, platform

    def test_discord_with_dm_channel_is_personal(self) -> None:
        """Discord DMs are personal."""
        classifier = PrivacyClassifier()
        frag = _make_fragment(platform=SourcePlatform.DISCORD, channel="dm-with-friend")
        assert classifier.classify_tier(frag) == PrivacyTier.PERSONAL

    def test_discord_with_named_public_channel_is_public(self) -> None:
        """Discord channels in named guild rooms are treated as PUBLIC."""
        classifier = PrivacyClassifier()
        frag = _make_fragment(platform=SourcePlatform.DISCORD, channel="general")
        assert classifier.classify_tier(frag) == PrivacyTier.PUBLIC

    def test_discord_without_channel_is_personal(self) -> None:
        """Discord without channel metadata defaults to PERSONAL (safer)."""
        classifier = PrivacyClassifier()
        frag = _make_fragment(platform=SourcePlatform.DISCORD)
        assert classifier.classify_tier(frag) == PrivacyTier.PERSONAL

    def test_recovery_content_is_intimate_regardless_of_source(self) -> None:
        """Recovery keywords elevate any fragment to INTIMATE."""
        classifier = PrivacyClassifier()
        frag = _make_fragment(platform=SourcePlatform.CLAUDE)
        text = "Talked with my sponsor about step work yesterday."
        assert classifier.classify_tier(frag, content=text) == PrivacyTier.INTIMATE

    def test_recovery_keyword_is_case_insensitive(self) -> None:
        """Keyword matching ignores case."""
        classifier = PrivacyClassifier()
        frag = _make_fragment(platform=SourcePlatform.CLAUDE)
        assert (
            classifier.classify_tier(frag, content="thoughts on Sobriety")
            == PrivacyTier.INTIMATE
        )

    def test_recovery_keyword_in_title(self) -> None:
        """Keywords found in the title also trigger INTIMATE."""
        classifier = PrivacyClassifier()
        frag = _make_fragment(
            title="On amends and grief",
            platform=SourcePlatform.CLAUDE,
        )
        assert classifier.classify_tier(frag) == PrivacyTier.INTIMATE

    def test_confessional_with_conviction_is_intimate(self) -> None:
        """Confessional voice with high confidence is INTIMATE."""
        classifier = PrivacyClassifier()
        frag = _make_fragment(
            register=VoiceRegister.CONFESSIONAL,
            confidence=Confidence.CONVICTION,
        )
        assert classifier.classify_tier(frag) == PrivacyTier.INTIMATE

    def test_confessional_without_high_confidence_is_personal(self) -> None:
        """A confessional whisper without conviction stays PERSONAL."""
        classifier = PrivacyClassifier()
        frag = _make_fragment(
            register=VoiceRegister.CONFESSIONAL,
            confidence=Confidence.EXPLORING,
        )
        assert classifier.classify_tier(frag) == PrivacyTier.PERSONAL

    def test_unknown_source_defaults_to_personal(self) -> None:
        """Unmapped sources default to PERSONAL — safer than PUBLIC."""
        classifier = PrivacyClassifier()
        frag = _make_fragment(platform=SourcePlatform.OTHER)
        assert classifier.classify_tier(frag) == PrivacyTier.PERSONAL

    def test_default_recovery_keyword_set_is_non_empty(self) -> None:
        """The shipped keyword set is documented and non-empty."""
        assert "recovery" in RECOVERY_KEYWORDS
        assert "sobriety" in RECOVERY_KEYWORDS
        assert "sponsor" in RECOVERY_KEYWORDS

    def test_custom_recovery_keywords(self) -> None:
        """Custom keyword sets are honoured for sensitive content detection."""
        classifier = PrivacyClassifier(recovery_keywords=frozenset({"crisis"}))
        frag = _make_fragment(platform=SourcePlatform.CLAUDE)
        assert (
            classifier.classify_tier(frag, content="this was a crisis moment")
            == PrivacyTier.INTIMATE
        )
        # Default keyword no longer triggers
        assert (
            classifier.classify_tier(frag, content="my sponsor texted me")
            == PrivacyTier.PERSONAL
        )


# ---- enforce_tier ----------------------------------------------------


class TestEnforceTier:
    """``enforce_tier`` applies tier-specific handling rules."""

    def test_intimate_disables_voice_proxy(self) -> None:
        """INTIMATE content is excluded from voice proxy generation."""
        classifier = PrivacyClassifier()
        frag = _make_fragment()
        result = classifier.enforce_tier(frag, PrivacyTier.INTIMATE)
        assert result.voice_proxy_eligible is False
        assert result.privacy_tier == PrivacyTier.INTIMATE

    def test_public_keeps_voice_proxy_enabled(self) -> None:
        """PUBLIC content remains voice-proxy eligible."""
        classifier = PrivacyClassifier()
        frag = _make_fragment()
        result = classifier.enforce_tier(frag, PrivacyTier.PUBLIC)
        assert result.voice_proxy_eligible is True
        assert result.privacy_tier == PrivacyTier.PUBLIC

    def test_personal_keeps_voice_proxy_enabled(self) -> None:
        """PERSONAL content remains voice-proxy eligible."""
        classifier = PrivacyClassifier()
        frag = _make_fragment()
        result = classifier.enforce_tier(frag, PrivacyTier.PERSONAL)
        assert result.voice_proxy_eligible is True
        assert result.privacy_tier == PrivacyTier.PERSONAL

    def test_returns_new_fragment_does_not_mutate_input(self) -> None:
        """``enforce_tier`` is non-mutating — it returns a copy."""
        classifier = PrivacyClassifier()
        frag = _make_fragment()
        before = frag.voice_proxy_eligible
        classifier.enforce_tier(frag, PrivacyTier.INTIMATE)
        assert frag.voice_proxy_eligible == before
        assert frag.privacy_tier == PrivacyTier.UNCLASSIFIED


# ---- classify_and_enforce -------------------------------------------


class TestClassifyAndEnforce:
    """``classify_and_enforce`` is the one-shot convenience method."""

    def test_journal_becomes_intimate_and_proxy_disabled(self) -> None:
        """A journal entry exits the classifier as INTIMATE + opted-out."""
        classifier = PrivacyClassifier()
        frag = _make_fragment(platform=SourcePlatform.JOURNAL)
        result = classifier.classify_and_enforce(frag)
        assert result.privacy_tier == PrivacyTier.INTIMATE
        assert result.voice_proxy_eligible is False

    def test_essay_becomes_public_and_proxy_eligible(self) -> None:
        """An essay exits as PUBLIC and voice-proxy eligible."""
        classifier = PrivacyClassifier()
        frag = _make_fragment(platform=SourcePlatform.ESSAY)
        result = classifier.classify_and_enforce(frag)
        assert result.privacy_tier == PrivacyTier.PUBLIC
        assert result.voice_proxy_eligible is True


# ---- ReviewQueueGenerator integration -------------------------------


class TestReviewQueueIntegration:
    """INTIMATE fragments are flagged for review automatically."""

    def test_intimate_fragment_needs_review(self) -> None:
        """INTIMATE always needs human review even with full classification."""
        generator = ReviewQueueGenerator()
        frag = Fragment(
            title="Personal note",
            source=FragmentSource(platform=SourcePlatform.JOURNAL),
            frequency=FrequencyClassification(primary=Frequency.F5),
            voice=VoiceClassification(confidence=Confidence.SETTLED),
            privacy_tier=PrivacyTier.INTIMATE,
        )
        assert generator.needs_review(frag) is True

    def test_public_classified_fragment_does_not_need_review(self) -> None:
        """A fully-classified PUBLIC fragment is not flagged."""
        generator = ReviewQueueGenerator()
        frag = Fragment(
            title="Published essay",
            source=FragmentSource(platform=SourcePlatform.ESSAY),
            frequency=FrequencyClassification(primary=Frequency.F3),
            voice=VoiceClassification(confidence=Confidence.SETTLED),
            privacy_tier=PrivacyTier.PUBLIC,
        )
        assert generator.needs_review(frag) is False


# ---- Module exports --------------------------------------------------


class TestModuleExports:
    """``creek.classify`` re-exports the privacy classifier."""

    def test_privacy_classifier_importable_from_package(self) -> None:
        """``from creek.classify import PrivacyClassifier`` works."""
        from creek.classify import (
            PrivacyClassifier as PrivacyClassifierFromPkg,
        )

        assert PrivacyClassifierFromPkg is PrivacyClassifier


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
