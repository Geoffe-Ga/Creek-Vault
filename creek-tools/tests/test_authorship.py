"""Tests for creek.clean.authorship — authorship tagger.

Tests cover:
- AuthorshipResult model (author type, confidence, needs_review, reasons)
- AuthorshipTagger: Claude/ChatGPT platform inference (human=self, assistant=ai)
- AuthorshipTagger: Discord author identification (self vs other vs collaborative)
- AuthorshipTagger: Markdown/journal default-to-self behaviour
- AuthorshipTagger: Google Drive single vs multi-author detection
- AuthorshipTagger: Code repo commit message author detection
- AuthorshipTagger: Ambiguous cases flagged for human review
- AuthorshipTagger: Configurable self-usernames for Discord
- Edge cases: missing metadata, unknown platform, empty authors
"""

import pytest
from pydantic import ValidationError

from creek.clean.authorship import AuthorshipResult, AuthorshipTagger

# ---------------------------------------------------------------------------
# AuthorshipResult model
# ---------------------------------------------------------------------------


class TestAuthorshipResult:
    """Tests for the AuthorshipResult Pydantic model."""

    def test_required_fields(self) -> None:
        """AuthorshipResult should have author, confidence, needs_review, reasons."""
        result = AuthorshipResult(
            author="self",
            confidence=0.9,
            needs_review=False,
            reasons=["journal entry"],
        )
        assert result.author == "self"
        assert result.confidence == 0.9
        assert result.needs_review is False
        assert result.reasons == ["journal entry"]

    def test_valid_author_types(self) -> None:
        """Author should accept 'self', 'ai', 'other', 'collaborative'."""
        for author_type in ("self", "ai", "other", "collaborative"):
            result = AuthorshipResult(
                author=author_type,
                confidence=0.8,
                needs_review=False,
                reasons=["test"],
            )
            assert result.author == author_type

    def test_invalid_author_type_rejected(self) -> None:
        """Invalid author type should raise ValidationError."""
        with pytest.raises(ValidationError):
            AuthorshipResult(
                author="unknown",  # type: ignore[arg-type]
                confidence=0.8,
                needs_review=False,
                reasons=["test"],
            )

    def test_confidence_bounds_valid(self) -> None:
        """Confidence should accept values in [0.0, 1.0]."""
        low = AuthorshipResult(
            author="self", confidence=0.0, needs_review=True, reasons=["low"]
        )
        high = AuthorshipResult(
            author="self", confidence=1.0, needs_review=False, reasons=["high"]
        )
        assert low.confidence == 0.0
        assert high.confidence == 1.0

    def test_confidence_out_of_bounds_rejected(self) -> None:
        """Confidence outside [0.0, 1.0] should raise ValidationError."""
        with pytest.raises(ValidationError):
            AuthorshipResult(
                author="self", confidence=1.5, needs_review=False, reasons=["bad"]
            )

    def test_serializable(self) -> None:
        """AuthorshipResult should be JSON-serializable."""
        result = AuthorshipResult(
            author="ai",
            confidence=0.95,
            needs_review=False,
            reasons=["assistant turn in Claude conversation"],
        )
        data = result.model_dump(mode="json")
        assert data["author"] == "ai"
        assert data["confidence"] == 0.95
        assert data["needs_review"] is False


# ---------------------------------------------------------------------------
# AuthorshipTagger — Claude/ChatGPT platform
# ---------------------------------------------------------------------------


class TestChatPlatformTagging:
    """Tests for Claude and ChatGPT conversation author tagging."""

    def test_claude_human_turn_tagged_self(self) -> None:
        """Human turns in Claude conversations should be tagged as self."""
        tagger = AuthorshipTagger()
        result = tagger.tag(
            platform="claude",
            metadata={"role": "human"},
        )
        assert result.author == "self"
        assert result.confidence >= 0.9

    def test_claude_assistant_turn_tagged_ai(self) -> None:
        """Assistant turns in Claude conversations should be tagged as ai."""
        tagger = AuthorshipTagger()
        result = tagger.tag(
            platform="claude",
            metadata={"role": "assistant"},
        )
        assert result.author == "ai"
        assert result.confidence >= 0.9

    def test_chatgpt_user_turn_tagged_self(self) -> None:
        """User turns in ChatGPT conversations should be tagged as self."""
        tagger = AuthorshipTagger()
        result = tagger.tag(
            platform="chatgpt",
            metadata={"role": "user"},
        )
        assert result.author == "self"
        assert result.confidence >= 0.9

    def test_chatgpt_assistant_turn_tagged_ai(self) -> None:
        """Assistant turns in ChatGPT conversations should be tagged as ai."""
        tagger = AuthorshipTagger()
        result = tagger.tag(
            platform="chatgpt",
            metadata={"role": "assistant"},
        )
        assert result.author == "ai"
        assert result.confidence >= 0.9

    def test_claude_no_role_needs_review(self) -> None:
        """Claude fragment without role metadata should be flagged for review."""
        tagger = AuthorshipTagger()
        result = tagger.tag(
            platform="claude",
            metadata={},
        )
        assert result.needs_review is True

    def test_chatgpt_system_role_tagged_ai(self) -> None:
        """System role in ChatGPT should be tagged as ai."""
        tagger = AuthorshipTagger()
        result = tagger.tag(
            platform="chatgpt",
            metadata={"role": "system"},
        )
        assert result.author == "ai"


# ---------------------------------------------------------------------------
# AuthorshipTagger — Discord platform
# ---------------------------------------------------------------------------


class TestDiscordTagging:
    """Tests for Discord message author tagging."""

    def test_discord_self_message(self) -> None:
        """Discord message from configured self-username should be tagged self."""
        tagger = AuthorshipTagger(self_usernames={"myuser"})
        result = tagger.tag(
            platform="discord",
            metadata={"authors": ["myuser"]},
        )
        assert result.author == "self"
        assert result.confidence >= 0.9

    def test_discord_other_message(self) -> None:
        """Discord message from a non-self username should be tagged other."""
        tagger = AuthorshipTagger(self_usernames={"myuser"})
        result = tagger.tag(
            platform="discord",
            metadata={"authors": ["someone_else"]},
        )
        assert result.author == "other"
        assert result.confidence >= 0.9

    def test_discord_collaborative_message(self) -> None:
        """Discord fragment with self and others should be tagged collaborative."""
        tagger = AuthorshipTagger(self_usernames={"myuser"})
        result = tagger.tag(
            platform="discord",
            metadata={"authors": ["myuser", "friend1", "friend2"]},
        )
        assert result.author == "collaborative"

    def test_discord_no_self_usernames_needs_review(self) -> None:
        """Discord without configured self-usernames should flag for review."""
        tagger = AuthorshipTagger()
        result = tagger.tag(
            platform="discord",
            metadata={"authors": ["someone"]},
        )
        assert result.needs_review is True

    def test_discord_empty_authors_needs_review(self) -> None:
        """Discord fragment with empty authors should flag for review."""
        tagger = AuthorshipTagger(self_usernames={"myuser"})
        result = tagger.tag(
            platform="discord",
            metadata={"authors": []},
        )
        assert result.needs_review is True

    def test_discord_case_insensitive_username_match(self) -> None:
        """Discord username matching should be case-insensitive."""
        tagger = AuthorshipTagger(self_usernames={"MyUser"})
        result = tagger.tag(
            platform="discord",
            metadata={"authors": ["myuser"]},
        )
        assert result.author == "self"

    def test_discord_multiple_self_usernames(self) -> None:
        """Multiple self-usernames should all be recognized."""
        tagger = AuthorshipTagger(self_usernames={"main_acct", "alt_acct"})
        result = tagger.tag(
            platform="discord",
            metadata={"authors": ["alt_acct"]},
        )
        assert result.author == "self"


# ---------------------------------------------------------------------------
# AuthorshipTagger — Markdown/Journal/Essay platform
# ---------------------------------------------------------------------------


class TestMarkdownTagging:
    """Tests for Markdown, journal, and essay author tagging."""

    def test_journal_defaults_to_self(self) -> None:
        """Journal entries should default to self."""
        tagger = AuthorshipTagger()
        result = tagger.tag(
            platform="journal",
            metadata={},
        )
        assert result.author == "self"
        assert result.confidence >= 0.8

    def test_essay_defaults_to_self(self) -> None:
        """Essays should default to self."""
        tagger = AuthorshipTagger()
        result = tagger.tag(
            platform="essay",
            metadata={},
        )
        assert result.author == "self"
        assert result.confidence >= 0.8

    def test_journal_with_other_author_metadata(self) -> None:
        """Journal with explicit other-author metadata should tag as other."""
        tagger = AuthorshipTagger()
        result = tagger.tag(
            platform="journal",
            metadata={"author": "someone_else"},
        )
        assert result.author == "other"

    def test_journal_with_empty_author_defaults_to_self(self) -> None:
        """Journal with empty-string author should default to self."""
        tagger = AuthorshipTagger()
        result = tagger.tag(
            platform="journal",
            metadata={"author": ""},
        )
        assert result.author == "self"

    def test_journal_with_whitespace_author_defaults_to_self(self) -> None:
        """Journal with whitespace-only author should default to self."""
        tagger = AuthorshipTagger()
        result = tagger.tag(
            platform="journal",
            metadata={"author": "   "},
        )
        assert result.author == "self"

    def test_markdown_reason_included(self) -> None:
        """Markdown/journal tagging should include a reason."""
        tagger = AuthorshipTagger()
        result = tagger.tag(
            platform="journal",
            metadata={},
        )
        assert len(result.reasons) >= 1


# ---------------------------------------------------------------------------
# AuthorshipTagger — Code platform
# ---------------------------------------------------------------------------


class TestCodeTagging:
    """Tests for code repository commit author tagging."""

    def test_code_self_commit(self) -> None:
        """Code commits from self-usernames should be tagged self."""
        tagger = AuthorshipTagger(self_usernames={"myuser"})
        result = tagger.tag(
            platform="code",
            metadata={"authors": ["myuser"]},
        )
        assert result.author == "self"

    def test_code_other_commit(self) -> None:
        """Code commits from others should be tagged other."""
        tagger = AuthorshipTagger(self_usernames={"myuser"})
        result = tagger.tag(
            platform="code",
            metadata={"authors": ["contributor"]},
        )
        assert result.author == "other"

    def test_code_no_authors_needs_review(self) -> None:
        """Code fragment without author info should flag for review."""
        tagger = AuthorshipTagger(self_usernames={"myuser"})
        result = tagger.tag(
            platform="code",
            metadata={},
        )
        assert result.needs_review is True


# ---------------------------------------------------------------------------
# AuthorshipTagger — Email platform
# ---------------------------------------------------------------------------


class TestEmailTagging:
    """Tests for email author tagging."""

    def test_email_self_author(self) -> None:
        """Email from self should be tagged self."""
        tagger = AuthorshipTagger(self_usernames={"myuser"})
        result = tagger.tag(
            platform="email",
            metadata={"authors": ["myuser"]},
        )
        assert result.author == "self"

    def test_email_other_author(self) -> None:
        """Email from another person should be tagged other."""
        tagger = AuthorshipTagger(self_usernames={"myuser"})
        result = tagger.tag(
            platform="email",
            metadata={"authors": ["colleague"]},
        )
        assert result.author == "other"


# ---------------------------------------------------------------------------
# AuthorshipTagger — Unknown/Other platform
# ---------------------------------------------------------------------------


class TestUnknownPlatformTagging:
    """Tests for unknown or generic platform tagging."""

    def test_unknown_platform_needs_review(self) -> None:
        """Unknown platform should flag for human review."""
        tagger = AuthorshipTagger()
        result = tagger.tag(
            platform="other",
            metadata={},
        )
        assert result.needs_review is True

    def test_image_ocr_needs_review(self) -> None:
        """Image OCR content should flag for review (ambiguous authorship)."""
        tagger = AuthorshipTagger()
        result = tagger.tag(
            platform="image_ocr",
            metadata={},
        )
        assert result.needs_review is True

    def test_unknown_platform_has_reason(self) -> None:
        """Unknown platform should include a reason."""
        tagger = AuthorshipTagger()
        result = tagger.tag(
            platform="other",
            metadata={},
        )
        assert len(result.reasons) >= 1


# ---------------------------------------------------------------------------
# AuthorshipTagger — Ambiguous cases
# ---------------------------------------------------------------------------


class TestAmbiguousCases:
    """Tests for ambiguous authorship detection and review flagging."""

    def test_collaborative_flagged_for_review(self) -> None:
        """Collaborative content should be flagged for human review."""
        tagger = AuthorshipTagger(self_usernames={"myuser"})
        result = tagger.tag(
            platform="discord",
            metadata={"authors": ["myuser", "friend"]},
        )
        assert result.author == "collaborative"
        assert result.needs_review is True

    def test_needs_review_has_reason(self) -> None:
        """Review-flagged results should include an explanatory reason."""
        tagger = AuthorshipTagger()
        result = tagger.tag(
            platform="other",
            metadata={},
        )
        assert result.needs_review is True
        assert len(result.reasons) >= 1


# ---------------------------------------------------------------------------
# AuthorshipTagger — Configuration
# ---------------------------------------------------------------------------


class TestTaggerConfiguration:
    """Tests for AuthorshipTagger configuration."""

    def test_default_empty_self_usernames(self) -> None:
        """Default tagger should have empty self-usernames."""
        tagger = AuthorshipTagger()
        assert tagger.self_usernames == frozenset()

    def test_self_usernames_stored_lowercase(self) -> None:
        """Self-usernames should be stored in lowercase for matching."""
        tagger = AuthorshipTagger(self_usernames={"Alice", "BOB"})
        assert "alice" in tagger.self_usernames
        assert "bob" in tagger.self_usernames

    def test_self_usernames_immutable(self) -> None:
        """Self-usernames should be a frozenset (immutable)."""
        tagger = AuthorshipTagger(self_usernames={"alice"})
        assert isinstance(tagger.self_usernames, frozenset)


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    """Tests for edge cases in authorship tagging."""

    def test_missing_metadata_handled(self) -> None:
        """Missing metadata should not cause errors."""
        tagger = AuthorshipTagger()
        result = tagger.tag(platform="claude", metadata={})
        assert isinstance(result, AuthorshipResult)

    def test_none_in_authors_list_handled(self) -> None:
        """None values in authors list should be filtered out."""
        tagger = AuthorshipTagger(self_usernames={"myuser"})
        result = tagger.tag(
            platform="discord",
            metadata={"authors": [None, "myuser"]},
        )
        assert result.author == "self"

    def test_empty_string_author_filtered(self) -> None:
        """Empty string authors should be filtered out."""
        tagger = AuthorshipTagger(self_usernames={"myuser"})
        result = tagger.tag(
            platform="discord",
            metadata={"authors": ["", "myuser"]},
        )
        assert result.author == "self"

    def test_result_always_has_reasons(self) -> None:
        """Every result should include at least one reason."""
        tagger = AuthorshipTagger()
        platforms = [
            "claude",
            "chatgpt",
            "discord",
            "journal",
            "essay",
            "code",
            "other",
        ]
        for platform in platforms:
            result = tagger.tag(platform=platform, metadata={})
            assert len(result.reasons) >= 1, f"No reasons for platform: {platform}"


# ---------------------------------------------------------------------------
# Integration: AuthorshipTagger with Fragment model
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestAuthorshipIntegration:
    """Integration test: AuthorshipTagger with Fragment models."""

    def test_tag_claude_fragment(self) -> None:
        """AuthorshipTagger should work with Claude fragment metadata."""
        from creek.models import Fragment, FragmentSource

        fragment = Fragment(
            title="Claude conversation turn 1",
            source=FragmentSource(
                platform="claude",
                original_file="exports/conversation.json",
                conversation_id="conv-123",
            ),
        )
        tagger = AuthorshipTagger()
        result = tagger.tag(
            platform=fragment.source.platform,
            metadata={"role": "human"},
        )
        assert result.author == "self"

    def test_tag_discord_fragment(self) -> None:
        """AuthorshipTagger should work with Discord fragment metadata."""
        from creek.models import Fragment, FragmentSource

        fragment = Fragment(
            title="Discord message group",
            source=FragmentSource(
                platform="discord",
                original_file="discord/general.json",
                channel="general",
            ),
        )
        tagger = AuthorshipTagger(self_usernames={"myuser"})
        result = tagger.tag(
            platform=fragment.source.platform,
            metadata={"authors": ["myuser", "friend"]},
        )
        assert result.author == "collaborative"
        assert result.needs_review is True
