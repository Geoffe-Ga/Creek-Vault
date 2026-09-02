"""Tests for the PrivacyClassifier (issue #27)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from creek.classify.privacy import RECOVERY_KEYWORDS, PrivacyClassifier
from creek.classify.review import ReviewQueueGenerator
from creek.models import (
    Authorship,
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
    author: Authorship = Authorship.SELF,
) -> Fragment:
    """Build a deterministic Fragment for tests."""
    from creek.models import synthetic_fragment_id

    return Fragment(
        id=synthetic_fragment_id(),
        title=title,
        source=FragmentSource(platform=platform, channel=channel, author=author),
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
        assert classifier.classify_tier(frag) == PrivacyTier.OPEN

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

    def test_discord_with_private_channel_hint_is_personal(self) -> None:
        """A channel name with a ``private`` token resolves to PERSONAL."""
        classifier = PrivacyClassifier()
        frag = _make_fragment(
            platform=SourcePlatform.DISCORD,
            channel="private-team-room",
        )
        assert classifier.classify_tier(frag) == PrivacyTier.PERSONAL

    def test_discord_with_named_public_channel_is_public(self) -> None:
        """Discord channels in named guild rooms are treated as PUBLIC."""
        classifier = PrivacyClassifier()
        frag = _make_fragment(platform=SourcePlatform.DISCORD, channel="general")
        assert classifier.classify_tier(frag) == PrivacyTier.OPEN

    def test_discord_admin_channel_is_not_falsely_personal(self) -> None:
        """``admin`` contains the ``dm`` substring but is not a DM token."""
        classifier = PrivacyClassifier()
        frag = _make_fragment(platform=SourcePlatform.DISCORD, channel="admin")
        assert classifier.classify_tier(frag) == PrivacyTier.OPEN

    def test_discord_without_channel_is_personal(self) -> None:
        """Discord without channel metadata defaults to PERSONAL (safer)."""
        classifier = PrivacyClassifier()
        frag = _make_fragment(platform=SourcePlatform.DISCORD)
        assert classifier.classify_tier(frag) == PrivacyTier.PERSONAL

    def test_recovery_content_in_self_authored_chatbot_is_intimate(self) -> None:
        """Recovery keywords on self-authored content elevate to INTIMATE."""
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

    def test_scoped_meeting_phrase_is_intimate(self) -> None:
        """``aa meeting`` is in-scope while bare ``meeting`` is not."""
        classifier = PrivacyClassifier()
        frag = _make_fragment(platform=SourcePlatform.CLAUDE)
        assert (
            classifier.classify_tier(frag, content="went to an AA meeting last night")
            == PrivacyTier.INTIMATE
        )

    def test_bare_meeting_does_not_trigger(self) -> None:
        """Plain ``meeting`` (work meetings, etc.) is not a recovery signal."""
        classifier = PrivacyClassifier()
        frag = _make_fragment(platform=SourcePlatform.CLAUDE)
        assert (
            classifier.classify_tier(frag, content="zoom meeting notes from monday")
            == PrivacyTier.PERSONAL
        )

    def test_scoped_inventory_phrase_is_intimate(self) -> None:
        """``moral inventory`` is in-scope; bare ``inventory`` is not."""
        classifier = PrivacyClassifier()
        frag = _make_fragment(platform=SourcePlatform.CLAUDE)
        assert (
            classifier.classify_tier(
                frag,
                content="working through my moral inventory this week",
            )
            == PrivacyTier.INTIMATE
        )

    def test_bare_inventory_does_not_trigger(self) -> None:
        """Plain ``inventory`` (stock, kitchen) is not a recovery signal."""
        classifier = PrivacyClassifier()
        frag = _make_fragment(platform=SourcePlatform.CLAUDE)
        assert (
            classifier.classify_tier(frag, content="kitchen inventory check")
            == PrivacyTier.PERSONAL
        )

    def test_ai_authored_recovery_content_is_personal_not_intimate(self) -> None:
        """AI-authored fragments are never elevated to INTIMATE by content.

        The PrivacyTier model docstring reserves INTIMATE for
        self-authored fragments. AI replies that mention recovery
        topics do not count as the user's intimate content.
        """
        classifier = PrivacyClassifier()
        frag = _make_fragment(platform=SourcePlatform.CLAUDE, author=Authorship.AI)
        assert (
            classifier.classify_tier(frag, content="my sponsor texted me about amends")
            == PrivacyTier.PERSONAL
        )

    def test_ai_authored_journal_does_not_become_intimate(self) -> None:
        """An AI-authored journal entry (unusual) is not silently elevated."""
        classifier = PrivacyClassifier()
        frag = _make_fragment(
            platform=SourcePlatform.JOURNAL,
            author=Authorship.AI,
        )
        assert classifier.classify_tier(frag) == PrivacyTier.PERSONAL

    def test_ai_authored_confessional_does_not_become_intimate(self) -> None:
        """A confessional voice attributed to AI does not elevate to INTIMATE."""
        classifier = PrivacyClassifier()
        frag = _make_fragment(
            platform=SourcePlatform.CLAUDE,
            register=VoiceRegister.CONFESSIONAL,
            confidence=Confidence.CONVICTION,
            author=Authorship.AI,
        )
        assert classifier.classify_tier(frag) == PrivacyTier.PERSONAL

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

    def test_confessional_without_confidence_value_is_personal(self) -> None:
        """A confessional register with no confidence stays PERSONAL."""
        classifier = PrivacyClassifier()
        frag = _make_fragment(
            register=VoiceRegister.CONFESSIONAL,
            confidence=None,
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
        assert "aa meeting" in RECOVERY_KEYWORDS
        assert "moral inventory" in RECOVERY_KEYWORDS

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
        result = classifier.enforce_tier(frag, PrivacyTier.OPEN)
        assert result.voice_proxy_eligible is True
        assert result.privacy_tier == PrivacyTier.OPEN

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

    def test_reclassify_intimate_to_personal_restores_voice_proxy(self) -> None:
        """Downgrading INTIMATE → PERSONAL must restore voice-proxy eligibility.

        Without an explicit reset, a fragment previously enforced as
        INTIMATE (voice_proxy_eligible=False) would silently stay opted
        out after re-classification — a correctness bug that would
        quietly remove user content from voice proxy generation.
        """
        classifier = PrivacyClassifier()
        frag = _make_fragment()
        intimate = classifier.enforce_tier(frag, PrivacyTier.INTIMATE)
        assert intimate.voice_proxy_eligible is False

        downgraded = classifier.enforce_tier(intimate, PrivacyTier.PERSONAL)
        assert downgraded.privacy_tier == PrivacyTier.PERSONAL
        assert downgraded.voice_proxy_eligible is True

    def test_reclassify_intimate_to_public_restores_voice_proxy(self) -> None:
        """Downgrading INTIMATE → PUBLIC also restores voice-proxy eligibility."""
        classifier = PrivacyClassifier()
        frag = _make_fragment()
        intimate = classifier.enforce_tier(frag, PrivacyTier.INTIMATE)
        promoted = classifier.enforce_tier(intimate, PrivacyTier.OPEN)
        assert promoted.privacy_tier == PrivacyTier.OPEN
        assert promoted.voice_proxy_eligible is True


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
        assert result.privacy_tier == PrivacyTier.OPEN
        assert result.voice_proxy_eligible is True

    def test_recovery_content_via_one_shot_method(self) -> None:
        """Recovery content reaches INTIMATE through the convenience method."""
        classifier = PrivacyClassifier()
        frag = _make_fragment(platform=SourcePlatform.CLAUDE)
        result = classifier.classify_and_enforce(
            frag,
            content="conversations with my sponsor today",
        )
        assert result.privacy_tier == PrivacyTier.INTIMATE
        assert result.voice_proxy_eligible is False

    def test_confessional_conviction_via_one_shot_method(self) -> None:
        """Confessional+conviction reaches INTIMATE through the convenience method."""
        classifier = PrivacyClassifier()
        frag = _make_fragment(
            register=VoiceRegister.CONFESSIONAL,
            confidence=Confidence.CONVICTION,
        )
        result = classifier.classify_and_enforce(frag)
        assert result.privacy_tier == PrivacyTier.INTIMATE
        assert result.voice_proxy_eligible is False


# ---- ReviewQueueGenerator integration -------------------------------


class TestReviewQueueIntegration:
    """INTIMATE fragments are flagged for review automatically."""

    def test_intimate_fragment_needs_review(self) -> None:
        """INTIMATE always needs human review even with full classification."""
        generator = ReviewQueueGenerator()
        frag = Fragment(
            id="frag-000000000018",
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
            id="frag-000000000017",
            title="Published essay",
            source=FragmentSource(platform=SourcePlatform.ESSAY),
            frequency=FrequencyClassification(primary=Frequency.F3),
            voice=VoiceClassification(confidence=Confidence.SETTLED),
            privacy_tier=PrivacyTier.OPEN,
        )
        assert generator.needs_review(frag) is False

    def test_an_unrecognised_tier_still_needs_review(self) -> None:
        """A tier the enum does not know must fail closed to review (#1489).

        Every other ``needs_review`` branch is deliberately neutralised —
        ``ESSAY`` is outside the default ``human_review_sources``, the
        frequency is classified, and ``SETTLED`` is not a low-confidence
        level — so the tier is the only thing left that can flag this
        fragment. Without that isolation the assertion would pass on the
        confidence branch and prove nothing about the tier reader.

        ``model_construct`` plants the tier without validation, the way a
        hand-edited vault note would.
        """
        generator = ReviewQueueGenerator()
        frag = Fragment.model_construct(
            id="frag-000000000019",
            title="Hand-edited note",
            source=FragmentSource(platform=SourcePlatform.ESSAY),
            frequency=FrequencyClassification(primary=Frequency.F3),
            voice=VoiceClassification(confidence=Confidence.SETTLED),
            privacy_tier="super-secret",
        )
        assert generator.needs_review(frag) is True


# ---- body_evidence_tier (#1136) ---------------------------------------


class TestBodyEvidenceTier:
    """``body_evidence_tier`` reports what the body *alone* proves.

    The aggregate ``classify_tier`` verdict saturates: on a self-authored
    journal entry the platform axis pins it at INTIMATE for every possible
    body, so comparing that verdict across an edit can never reveal that the
    edit added privacy-relevant material. This narrower question is what
    ``privacy_pass.edit_added_evidence`` uses to see past the saturation.
    """

    def test_self_authored_recovery_body_is_intimate(self) -> None:
        """Recovery vocabulary in the body is INTIMATE evidence."""
        classifier = PrivacyClassifier()
        frag = _make_fragment(platform=SourcePlatform.ESSAY)
        assert (
            classifier.body_evidence_tier(frag, "my sponsor called about my relapse")
            is PrivacyTier.INTIMATE
        )

    def test_benign_body_proves_nothing(self) -> None:
        """``None`` is not OPEN — it means the body makes no claim at all.

        Reporting OPEN here would be a *claim* that the body is publishable,
        which a caller merging tiers could act on. The distinction is the
        whole point of the return type.
        """
        classifier = PrivacyClassifier()
        frag = _make_fragment(platform=SourcePlatform.JOURNAL)
        assert (
            classifier.body_evidence_tier(frag, "bread, water, salt, patience") is None
        )
        assert (
            classifier.classify_tier(frag, content="bread, water, salt, patience")
            is PrivacyTier.INTIMATE
        )

    def test_recovery_in_the_title_counts(self) -> None:
        """The scan covers title + body, matching ``classify_tier``'s check 1."""
        classifier = PrivacyClassifier()
        frag = _make_fragment(title="Notes on sobriety", platform=SourcePlatform.ESSAY)
        assert (
            classifier.body_evidence_tier(frag, "a plain day") is PrivacyTier.INTIMATE
        )

    def test_ai_authored_recovery_body_proves_nothing(self) -> None:
        """The Authorship.SELF gate lives here, not in the callers.

        A body-evidence question that ignored authorship would license
        escalating an AI-authored fragment to INTIMATE on content signals,
        which the ``PrivacyTier`` model docstring forbids.
        """
        classifier = PrivacyClassifier()
        frag = _make_fragment(platform=SourcePlatform.CHATGPT, author=Authorship.AI)
        assert classifier.body_evidence_tier(frag, "notes on my relapse") is None

    def test_honours_a_custom_keyword_set(self) -> None:
        """It reads ``self.recovery_keywords``, not the module constant."""
        classifier = PrivacyClassifier(recovery_keywords=frozenset({"weathervane"}))
        frag = _make_fragment(platform=SourcePlatform.ESSAY)
        assert classifier.body_evidence_tier(frag, "the weathervane turned") is (
            PrivacyTier.INTIMATE
        )
        assert classifier.body_evidence_tier(frag, "notes on my relapse") is None

    @pytest.mark.parametrize("platform", list(SourcePlatform))
    @pytest.mark.parametrize("author", list(Authorship))
    def test_bodies_agreeing_on_evidence_classify_alike(
        self, platform: SourcePlatform, author: Authorship
    ) -> None:
        """Two bodies with the same evidence verdict get the same tier.

        The invariant ``edit_added_evidence``'s second disjunct rests on:
        ``body_evidence_tier`` is the *only* body-derived input to
        ``classify_tier``. If someone adds a second rule that reads
        ``content`` without routing it through ``body_evidence_tier``, the
        gate would silently stop detecting that rule's evidence — and this
        parametrised sweep over every platform x authorship pair fails
        loudly on the day that happens, rather than at the next privacy
        incident.
        """
        classifier = PrivacyClassifier()
        frag = _make_fragment(platform=platform, author=author, channel="general")
        left = "a plain note about the walk to the shops"
        right = "an entirely different note concerning bread and its patience"
        assert classifier.body_evidence_tier(frag, left) is None
        assert classifier.body_evidence_tier(frag, right) is None
        assert classifier.classify_tier(frag, content=left) is classifier.classify_tier(
            frag, content=right
        )


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
