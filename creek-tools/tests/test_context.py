"""Tests for creek.clean.context — non-user content handling.

Tests cover:
- ContextResult model structure and serialization
- ContextExtractor with context_metadata mode (default)
- ContextExtractor with low_priority mode
- ContextExtractor with skip mode
- Voice proxy exclusion enforced across all modes
- Privacy tier restrictions (others' content never Intimate)
- Attribution preservation across all modes
- Conversation flow / reply chain preservation in context mode
- Edge cases: empty input, all-self, all-other, mixed authorship
- Configurable quality penalty for low_priority mode
"""

from datetime import UTC, datetime

import pytest

from creek.clean.context import ContextExtractor, ContextResult
from creek.config import ContextConfig
from creek.models import (
    Authorship,
    Fragment,
    FragmentSource,
    PrivacyTier,
    SourcePlatform,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_fragment(
    title: str,
    author: Authorship = Authorship.SELF,
    platform: SourcePlatform = SourcePlatform.DISCORD,
    *,
    conversation_id: str | None = None,
    privacy_tier: PrivacyTier = PrivacyTier.UNCLASSIFIED,
) -> Fragment:
    """Create a test fragment with the given authorship."""
    from creek.models import synthetic_fragment_id

    return Fragment(
        id=synthetic_fragment_id(),
        title=title,
        source=FragmentSource(
            platform=platform,
            author=author,
            conversation_id=conversation_id,
        ),
        created=datetime(2024, 1, 15, 12, 0, tzinfo=UTC),
        privacy_tier=privacy_tier,
    )


# ---------------------------------------------------------------------------
# ContextResult model
# ---------------------------------------------------------------------------


class TestContextResult:
    """Tests for the ContextResult model."""

    def test_required_fields(self) -> None:
        """ContextResult should have kept, dropped, and context_entries fields."""
        result = ContextResult(kept=[], dropped=[], context_entries=0)
        assert result.kept == []
        assert result.dropped == []
        assert result.context_entries == 0

    def test_serializable(self) -> None:
        """ContextResult should be JSON-serializable."""
        frag = _make_fragment("test")
        result = ContextResult(kept=[frag], dropped=[], context_entries=0)
        data = result.model_dump(mode="json")
        assert isinstance(data["kept"], list)
        assert len(data["kept"]) == 1

    def test_context_entries_count(self) -> None:
        """ContextResult should track number of context entries added."""
        result = ContextResult(kept=[], dropped=[], context_entries=3)
        assert result.context_entries == 3


# ---------------------------------------------------------------------------
# ContextExtractor — context_metadata mode (default)
# ---------------------------------------------------------------------------


class TestContextMetadataMode:
    """Tests for context_metadata handling mode."""

    def test_default_mode_is_context_metadata(self) -> None:
        """ContextExtractor should default to context_metadata mode."""
        extractor = ContextExtractor()
        assert extractor.mode == "context_metadata"

    def test_self_fragments_kept(self) -> None:
        """Self-authored fragments should be kept in context_metadata mode."""
        extractor = ContextExtractor()
        frags = [_make_fragment("my thought", Authorship.SELF)]
        result = extractor.extract(frags)
        assert len(result.kept) == 1
        assert result.kept[0].title == "my thought"

    def test_other_fragments_dropped(self) -> None:
        """Others' fragments should be dropped in context_metadata mode."""
        extractor = ContextExtractor()
        frags = [_make_fragment("their message", Authorship.OTHER)]
        result = extractor.extract(frags)
        assert len(result.kept) == 0
        assert len(result.dropped) == 1

    def test_context_stored_in_user_fragment(self) -> None:
        """Others' content should be stored as context in adjacent user fragment."""
        extractor = ContextExtractor()
        frags = [
            _make_fragment(
                "their question",
                Authorship.OTHER,
                conversation_id="conv-1",
            ),
            _make_fragment(
                "my reply",
                Authorship.SELF,
                conversation_id="conv-1",
            ),
        ]
        result = extractor.extract(frags)
        assert len(result.kept) == 1
        assert result.kept[0].title == "my reply"
        assert len(result.kept[0].context) > 0
        assert "their question" in result.kept[0].context[0]
        assert result.context_entries == 1

    def test_conversation_flow_preserved(self) -> None:
        """Reply chains should preserve conversation flow order."""
        extractor = ContextExtractor()
        frags = [
            _make_fragment(
                "first other msg",
                Authorship.OTHER,
                conversation_id="conv-1",
            ),
            _make_fragment(
                "second other msg",
                Authorship.OTHER,
                conversation_id="conv-1",
            ),
            _make_fragment(
                "my response",
                Authorship.SELF,
                conversation_id="conv-1",
            ),
        ]
        result = extractor.extract(frags)
        assert len(result.kept) == 1
        assert len(result.kept[0].context) == 2
        assert "first other msg" in result.kept[0].context[0]
        assert "second other msg" in result.kept[0].context[1]

    def test_ai_fragments_treated_as_context(self) -> None:
        """AI fragments should also be stored as context, not indexed."""
        extractor = ContextExtractor()
        frags = [
            _make_fragment("AI response", Authorship.AI, conversation_id="conv-1"),
            _make_fragment("my follow-up", Authorship.SELF, conversation_id="conv-1"),
        ]
        result = extractor.extract(frags)
        assert len(result.kept) == 1
        assert len(result.kept[0].context) == 1

    def test_collaborative_fragments_kept(self) -> None:
        """Collaborative fragments should be kept but marked voice-proxy ineligible."""
        extractor = ContextExtractor()
        frags = [_make_fragment("joint work", Authorship.COLLABORATIVE)]
        result = extractor.extract(frags)
        assert len(result.kept) == 1
        assert result.kept[0].voice_proxy_eligible is False


# ---------------------------------------------------------------------------
# ContextExtractor — low_priority mode
# ---------------------------------------------------------------------------


class TestLowPriorityMode:
    """Tests for low_priority handling mode."""

    def test_self_fragments_unchanged(self) -> None:
        """Self-authored fragments should be unmodified in low_priority mode."""
        config = ContextConfig(mode="low_priority")
        extractor = ContextExtractor(config=config)
        frags = [_make_fragment("my thought", Authorship.SELF)]
        result = extractor.extract(frags)
        assert len(result.kept) == 1
        assert result.kept[0].voice_proxy_eligible is True

    def test_other_fragments_kept_with_reduced_priority(self) -> None:
        """Others' fragments should be kept but with voice_proxy_eligible=False."""
        config = ContextConfig(mode="low_priority")
        extractor = ContextExtractor(config=config)
        frags = [_make_fragment("their message", Authorship.OTHER)]
        result = extractor.extract(frags)
        assert len(result.kept) == 1
        assert result.kept[0].voice_proxy_eligible is False
        assert len(result.dropped) == 0

    def test_ai_fragments_kept_voice_proxy_excluded(self) -> None:
        """AI fragments should be kept but excluded from voice proxy."""
        config = ContextConfig(mode="low_priority")
        extractor = ContextExtractor(config=config)
        frags = [_make_fragment("AI response", Authorship.AI)]
        result = extractor.extract(frags)
        assert len(result.kept) == 1
        assert result.kept[0].voice_proxy_eligible is False

    def test_quality_penalty_applied(self) -> None:
        """Others' fragments should have reduced quality indicator via tags."""
        config = ContextConfig(mode="low_priority", quality_penalty=0.5)
        extractor = ContextExtractor(config=config)
        frags = [_make_fragment("their content", Authorship.OTHER)]
        result = extractor.extract(frags)
        assert len(result.kept) == 1
        assert "low-priority" in result.kept[0].tags

    def test_custom_quality_penalty(self) -> None:
        """Quality penalty should be configurable."""
        config = ContextConfig(mode="low_priority", quality_penalty=0.3)
        extractor = ContextExtractor(config=config)
        assert extractor.quality_penalty == 0.3

    def test_collaborative_kept_voice_proxy_excluded(self) -> None:
        """Collaborative fragments should be kept but voice-proxy ineligible."""
        config = ContextConfig(mode="low_priority")
        extractor = ContextExtractor(config=config)
        frags = [_make_fragment("joint work", Authorship.COLLABORATIVE)]
        result = extractor.extract(frags)
        assert len(result.kept) == 1
        assert result.kept[0].voice_proxy_eligible is False


# ---------------------------------------------------------------------------
# ContextExtractor — skip mode
# ---------------------------------------------------------------------------


class TestSkipMode:
    """Tests for skip handling mode."""

    def test_self_fragments_kept(self) -> None:
        """Self-authored fragments should be kept in skip mode."""
        config = ContextConfig(mode="skip")
        extractor = ContextExtractor(config=config)
        frags = [_make_fragment("my thought", Authorship.SELF)]
        result = extractor.extract(frags)
        assert len(result.kept) == 1

    def test_other_fragments_dropped(self) -> None:
        """Others' fragments should be dropped entirely in skip mode."""
        config = ContextConfig(mode="skip")
        extractor = ContextExtractor(config=config)
        frags = [_make_fragment("their message", Authorship.OTHER)]
        result = extractor.extract(frags)
        assert len(result.kept) == 0
        assert len(result.dropped) == 1

    def test_ai_fragments_dropped(self) -> None:
        """AI fragments should be dropped in skip mode."""
        config = ContextConfig(mode="skip")
        extractor = ContextExtractor(config=config)
        frags = [_make_fragment("AI response", Authorship.AI)]
        result = extractor.extract(frags)
        assert len(result.kept) == 0
        assert len(result.dropped) == 1

    def test_collaborative_fragments_kept(self) -> None:
        """Collaborative fragments should be kept even in skip mode."""
        config = ContextConfig(mode="skip")
        extractor = ContextExtractor(config=config)
        frags = [_make_fragment("joint work", Authorship.COLLABORATIVE)]
        result = extractor.extract(frags)
        assert len(result.kept) == 1
        assert result.kept[0].voice_proxy_eligible is False

    def test_no_context_added_in_skip_mode(self) -> None:
        """Skip mode should not add context entries to user fragments."""
        config = ContextConfig(mode="skip")
        extractor = ContextExtractor(config=config)
        frags = [
            _make_fragment(
                "their question",
                Authorship.OTHER,
                conversation_id="conv-1",
            ),
            _make_fragment(
                "my reply",
                Authorship.SELF,
                conversation_id="conv-1",
            ),
        ]
        result = extractor.extract(frags)
        assert len(result.kept) == 1
        assert len(result.kept[0].context) == 0


# ---------------------------------------------------------------------------
# Universal constraints
# ---------------------------------------------------------------------------


class TestUniversalConstraints:
    """Tests for constraints that apply across all modes."""

    def test_voice_proxy_excluded_for_other_context_metadata(self) -> None:
        """Others' content never used for voice proxy — context_metadata mode."""
        extractor = ContextExtractor()
        frags = [_make_fragment("their msg", Authorship.OTHER)]
        result = extractor.extract(frags)
        # In context_metadata, others are dropped so not in kept at all
        assert all(f.source.author != "other" for f in result.kept)

    def test_voice_proxy_excluded_for_other_low_priority(self) -> None:
        """Others' content never used for voice proxy — low_priority mode."""
        config = ContextConfig(mode="low_priority")
        extractor = ContextExtractor(config=config)
        frags = [_make_fragment("their msg", Authorship.OTHER)]
        result = extractor.extract(frags)
        for frag in result.kept:
            if frag.source.author == "other":
                assert frag.voice_proxy_eligible is False

    def test_intimate_privacy_blocked_for_other(self) -> None:
        """Others' content must never be classified as Intimate privacy tier."""
        config = ContextConfig(mode="low_priority")
        extractor = ContextExtractor(config=config)
        frags = [
            _make_fragment(
                "their msg",
                Authorship.OTHER,
                privacy_tier=PrivacyTier.INTIMATE,
            ),
        ]
        result = extractor.extract(frags)
        for frag in result.kept:
            if frag.source.author == "other":
                assert frag.privacy_tier != "intimate"

    def test_intimate_privacy_blocked_for_ai(self) -> None:
        """AI content must never be classified as Intimate privacy tier."""
        config = ContextConfig(mode="low_priority")
        extractor = ContextExtractor(config=config)
        frags = [
            _make_fragment(
                "AI msg",
                Authorship.AI,
                privacy_tier=PrivacyTier.INTIMATE,
            ),
        ]
        result = extractor.extract(frags)
        for frag in result.kept:
            if frag.source.author == "ai":
                assert frag.privacy_tier != "intimate"

    def test_intimate_privacy_allowed_for_self(self) -> None:
        """Self-authored content should be allowed Intimate privacy tier."""
        config = ContextConfig(mode="low_priority")
        extractor = ContextExtractor(config=config)
        frags = [
            _make_fragment(
                "my private thought",
                Authorship.SELF,
                privacy_tier=PrivacyTier.INTIMATE,
            ),
        ]
        result = extractor.extract(frags)
        assert result.kept[0].privacy_tier == "intimate"

    def test_attribution_preserved_context_metadata(self) -> None:
        """Attribution should be preserved in context entries."""
        extractor = ContextExtractor()
        frags = [
            _make_fragment(
                "their question",
                Authorship.OTHER,
                conversation_id="conv-1",
            ),
            _make_fragment(
                "my reply",
                Authorship.SELF,
                conversation_id="conv-1",
            ),
        ]
        result = extractor.extract(frags)
        # Context entry should contain attribution info
        assert any("other" in c.lower() for c in result.kept[0].context)

    def test_attribution_preserved_low_priority(self) -> None:
        """Author field should remain set for low_priority fragments."""
        config = ContextConfig(mode="low_priority")
        extractor = ContextExtractor(config=config)
        frags = [_make_fragment("their msg", Authorship.OTHER)]
        result = extractor.extract(frags)
        assert result.kept[0].source.author == "other"


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    """Tests for edge cases and boundary conditions."""

    def test_empty_input(self) -> None:
        """Empty input should return empty result."""
        extractor = ContextExtractor()
        result = extractor.extract([])
        assert len(result.kept) == 0
        assert len(result.dropped) == 0
        assert result.context_entries == 0

    def test_all_self_fragments(self) -> None:
        """All-self input should pass through unchanged."""
        extractor = ContextExtractor()
        frags = [
            _make_fragment("thought 1", Authorship.SELF),
            _make_fragment("thought 2", Authorship.SELF),
        ]
        result = extractor.extract(frags)
        assert len(result.kept) == 2
        assert len(result.dropped) == 0

    def test_all_other_fragments_context_metadata(self) -> None:
        """All-other input in context_metadata should drop all."""
        extractor = ContextExtractor()
        frags = [
            _make_fragment("msg 1", Authorship.OTHER),
            _make_fragment("msg 2", Authorship.OTHER),
        ]
        result = extractor.extract(frags)
        assert len(result.kept) == 0
        assert len(result.dropped) == 2

    def test_mixed_conversation_context_metadata(self) -> None:
        """Mixed conversation should properly distribute context."""
        extractor = ContextExtractor()
        frags = [
            _make_fragment("other 1", Authorship.OTHER, conversation_id="c1"),
            _make_fragment("self 1", Authorship.SELF, conversation_id="c1"),
            _make_fragment("other 2", Authorship.OTHER, conversation_id="c1"),
            _make_fragment("self 2", Authorship.SELF, conversation_id="c1"),
        ]
        result = extractor.extract(frags)
        assert len(result.kept) == 2
        # First self fragment gets context from other 1
        assert len(result.kept[0].context) == 1
        # Second self fragment gets context from other 2
        assert len(result.kept[1].context) == 1

    def test_no_conversation_id_context_not_linked(self) -> None:
        """Fragments without conversation_id should not get cross-linked context."""
        extractor = ContextExtractor()
        frags = [
            _make_fragment("other msg", Authorship.OTHER),
            _make_fragment("self msg", Authorship.SELF),
        ]
        result = extractor.extract(frags)
        assert len(result.kept) == 1
        # No conversation_id means no context linking
        assert len(result.kept[0].context) == 0

    def test_different_conversations_not_mixed(self) -> None:
        """Context from different conversations should not be mixed."""
        extractor = ContextExtractor()
        frags = [
            _make_fragment(
                "other in conv-a",
                Authorship.OTHER,
                conversation_id="conv-a",
            ),
            _make_fragment(
                "self in conv-b",
                Authorship.SELF,
                conversation_id="conv-b",
            ),
        ]
        result = extractor.extract(frags)
        assert len(result.kept) == 1
        assert len(result.kept[0].context) == 0


# ---------------------------------------------------------------------------
# Config validation
# ---------------------------------------------------------------------------


class TestContextConfig:
    """Tests for ContextConfig validation."""

    def test_default_mode(self) -> None:
        """Default mode should be context_metadata."""
        config = ContextConfig()
        assert config.mode == "context_metadata"

    def test_valid_modes(self) -> None:
        """All three modes should be accepted."""
        for mode in ("context_metadata", "low_priority", "skip"):
            config = ContextConfig(mode=mode)
            assert config.mode == mode

    def test_invalid_mode_rejected(self) -> None:
        """Invalid mode should raise ValueError."""
        with pytest.raises(ValueError, match="Invalid context mode"):
            ContextConfig(mode="invalid")

    def test_default_quality_penalty(self) -> None:
        """Default quality penalty should be 0.5."""
        config = ContextConfig()
        assert config.quality_penalty == 0.5

    def test_quality_penalty_bounds(self) -> None:
        """Quality penalty outside [0.0, 1.0] should raise ValueError."""
        with pytest.raises(ValueError, match="quality_penalty"):
            ContextConfig(quality_penalty=1.5)
        with pytest.raises(ValueError, match="quality_penalty"):
            ContextConfig(quality_penalty=-0.1)
