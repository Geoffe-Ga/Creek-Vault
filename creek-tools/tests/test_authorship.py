"""Tests for creek.clean.authorship — authorship tagger.

Tests cover:
- Authorship StrEnum values (self, ai, other, collaborative)
- FragmentSource.author field (optional, defaults None)
- AuthorshipTagger.tag() for Claude conversations (human=self, assistant=ai)
- AuthorshipTagger.tag() for ChatGPT conversations (human=self, assistant=ai)
- AuthorshipTagger.tag() for Discord (own messages=self, others=other)
- AuthorshipTagger.tag() for Journal/Essay/Code (default self)
- AuthorshipTagger.tag() for Email/Image OCR/Other (return None)
- Configurable discord_username for self-identification
- Ambiguous cases return None
- Edge cases: missing interlocutor, unknown role strings
"""

from creek.clean.authorship import AuthorshipTagger
from creek.models import Authorship, Fragment, FragmentSource, SourcePlatform

# ---------------------------------------------------------------------------
# Authorship enum
# ---------------------------------------------------------------------------


class TestAuthorshipEnum:
    """Tests for the Authorship StrEnum."""

    def test_self_value(self) -> None:
        """Authorship.SELF should have string value 'self'."""
        assert Authorship.SELF == "self"

    def test_ai_value(self) -> None:
        """Authorship.AI should have string value 'ai'."""
        assert Authorship.AI == "ai"

    def test_other_value(self) -> None:
        """Authorship.OTHER should have string value 'other'."""
        assert Authorship.OTHER == "other"

    def test_collaborative_value(self) -> None:
        """Authorship.COLLABORATIVE should have string value 'collaborative'."""
        assert Authorship.COLLABORATIVE == "collaborative"

    def test_all_values(self) -> None:
        """Authorship should have exactly four members."""
        assert len(Authorship) == 4

    def test_is_str_enum(self) -> None:
        """Authorship values should be usable as strings."""
        assert isinstance(Authorship.SELF, str)


# ---------------------------------------------------------------------------
# FragmentSource.author field
# ---------------------------------------------------------------------------


class TestFragmentSourceAuthorField:
    """Tests for the optional author field on FragmentSource."""

    def test_author_defaults_none(self) -> None:
        """FragmentSource.author should default to None."""
        source = FragmentSource(platform=SourcePlatform.JOURNAL)
        assert source.author is None

    def test_author_accepts_authorship_value(self) -> None:
        """FragmentSource.author should accept an Authorship value."""
        source = FragmentSource(
            platform=SourcePlatform.CLAUDE,
            author=Authorship.AI,
        )
        assert source.author == "ai"

    def test_author_serializes_to_string(self) -> None:
        """FragmentSource.author should serialize as a string."""
        source = FragmentSource(
            platform=SourcePlatform.JOURNAL,
            author=Authorship.SELF,
        )
        data = source.model_dump(mode="json")
        assert data["author"] == "self"

    def test_author_none_serializes_to_null(self) -> None:
        """FragmentSource.author None should serialize as null."""
        source = FragmentSource(platform=SourcePlatform.OTHER)
        data = source.model_dump(mode="json")
        assert data["author"] is None


# ---------------------------------------------------------------------------
# AuthorshipTagger — Claude conversations
# ---------------------------------------------------------------------------


class TestAuthorshipTaggerClaude:
    """Tests for authorship tagging of Claude conversation fragments."""

    def test_claude_human_turn_is_self(self) -> None:
        """Claude human turn should be tagged as self."""
        tagger = AuthorshipTagger()
        fragment = Fragment(
            title="Human turn",
            source=FragmentSource(
                platform=SourcePlatform.CLAUDE,
                interlocutor="human",
            ),
        )
        result = tagger.tag(fragment)
        assert result == Authorship.SELF

    def test_claude_assistant_turn_is_ai(self) -> None:
        """Claude assistant turn should be tagged as ai."""
        tagger = AuthorshipTagger()
        fragment = Fragment(
            title="Assistant turn",
            source=FragmentSource(
                platform=SourcePlatform.CLAUDE,
                interlocutor="assistant",
            ),
        )
        result = tagger.tag(fragment)
        assert result == Authorship.AI

    def test_claude_no_interlocutor_is_none(self) -> None:
        """Claude fragment with no interlocutor should return None."""
        tagger = AuthorshipTagger()
        fragment = Fragment(
            title="Unknown turn",
            source=FragmentSource(platform=SourcePlatform.CLAUDE),
        )
        result = tagger.tag(fragment)
        assert result is None

    def test_claude_unknown_role_is_none(self) -> None:
        """Claude fragment with an unrecognized role should return None."""
        tagger = AuthorshipTagger()
        fragment = Fragment(
            title="System message",
            source=FragmentSource(
                platform=SourcePlatform.CLAUDE,
                interlocutor="system",
            ),
        )
        result = tagger.tag(fragment)
        assert result is None


# ---------------------------------------------------------------------------
# AuthorshipTagger — ChatGPT conversations
# ---------------------------------------------------------------------------


class TestAuthorshipTaggerChatGPT:
    """Tests for authorship tagging of ChatGPT conversation fragments."""

    def test_chatgpt_user_turn_is_self(self) -> None:
        """ChatGPT user turn should be tagged as self."""
        tagger = AuthorshipTagger()
        fragment = Fragment(
            title="User turn",
            source=FragmentSource(
                platform=SourcePlatform.CHATGPT,
                interlocutor="user",
            ),
        )
        result = tagger.tag(fragment)
        assert result == Authorship.SELF

    def test_chatgpt_human_turn_is_self(self) -> None:
        """ChatGPT human turn should also be tagged as self."""
        tagger = AuthorshipTagger()
        fragment = Fragment(
            title="Human turn",
            source=FragmentSource(
                platform=SourcePlatform.CHATGPT,
                interlocutor="human",
            ),
        )
        result = tagger.tag(fragment)
        assert result == Authorship.SELF

    def test_chatgpt_assistant_turn_is_ai(self) -> None:
        """ChatGPT assistant turn should be tagged as ai."""
        tagger = AuthorshipTagger()
        fragment = Fragment(
            title="Assistant turn",
            source=FragmentSource(
                platform=SourcePlatform.CHATGPT,
                interlocutor="assistant",
            ),
        )
        result = tagger.tag(fragment)
        assert result == Authorship.AI

    def test_chatgpt_no_interlocutor_is_none(self) -> None:
        """ChatGPT fragment with no interlocutor should return None."""
        tagger = AuthorshipTagger()
        fragment = Fragment(
            title="Unknown turn",
            source=FragmentSource(platform=SourcePlatform.CHATGPT),
        )
        result = tagger.tag(fragment)
        assert result is None

    def test_chatgpt_unknown_role_is_none(self) -> None:
        """ChatGPT fragment with an unrecognized role should return None."""
        tagger = AuthorshipTagger()
        fragment = Fragment(
            title="Tool message",
            source=FragmentSource(
                platform=SourcePlatform.CHATGPT,
                interlocutor="tool",
            ),
        )
        result = tagger.tag(fragment)
        assert result is None


# ---------------------------------------------------------------------------
# AuthorshipTagger — Discord messages
# ---------------------------------------------------------------------------


class TestAuthorshipTaggerDiscord:
    """Tests for authorship tagging of Discord message fragments."""

    def test_discord_own_message_is_self(self) -> None:
        """Discord message from own username should be tagged as self."""
        tagger = AuthorshipTagger()
        fragment = Fragment(
            title="My message",
            source=FragmentSource(
                platform=SourcePlatform.DISCORD,
                interlocutor="myuser",
            ),
        )
        result = tagger.tag(fragment, discord_username="myuser")
        assert result == Authorship.SELF

    def test_discord_other_message_is_other(self) -> None:
        """Discord message from another user should be tagged as other."""
        tagger = AuthorshipTagger()
        fragment = Fragment(
            title="Someone's message",
            source=FragmentSource(
                platform=SourcePlatform.DISCORD,
                interlocutor="frienduser",
            ),
        )
        result = tagger.tag(fragment, discord_username="myuser")
        assert result == Authorship.OTHER

    def test_discord_no_username_is_none(self) -> None:
        """Discord message without discord_username config returns None."""
        tagger = AuthorshipTagger()
        fragment = Fragment(
            title="Message",
            source=FragmentSource(
                platform=SourcePlatform.DISCORD,
                interlocutor="someone",
            ),
        )
        result = tagger.tag(fragment)
        assert result is None

    def test_discord_no_interlocutor_is_none(self) -> None:
        """Discord message with no interlocutor should return None."""
        tagger = AuthorshipTagger()
        fragment = Fragment(
            title="Message",
            source=FragmentSource(platform=SourcePlatform.DISCORD),
        )
        result = tagger.tag(fragment, discord_username="myuser")
        assert result is None

    def test_discord_case_insensitive_username(self) -> None:
        """Discord username matching should be case-insensitive."""
        tagger = AuthorshipTagger()
        fragment = Fragment(
            title="My message",
            source=FragmentSource(
                platform=SourcePlatform.DISCORD,
                interlocutor="MyUser",
            ),
        )
        result = tagger.tag(fragment, discord_username="myuser")
        assert result == Authorship.SELF


# ---------------------------------------------------------------------------
# AuthorshipTagger — Default self platforms
# ---------------------------------------------------------------------------


class TestAuthorshipTaggerDefaultSelf:
    """Tests for platforms that default to self authorship."""

    def test_journal_is_self(self) -> None:
        """Journal fragments should default to self."""
        tagger = AuthorshipTagger()
        fragment = Fragment(
            title="Journal entry",
            source=FragmentSource(platform=SourcePlatform.JOURNAL),
        )
        result = tagger.tag(fragment)
        assert result == Authorship.SELF

    def test_essay_is_self(self) -> None:
        """Essay fragments should default to self."""
        tagger = AuthorshipTagger()
        fragment = Fragment(
            title="Essay draft",
            source=FragmentSource(platform=SourcePlatform.ESSAY),
        )
        result = tagger.tag(fragment)
        assert result == Authorship.SELF

    def test_code_is_self(self) -> None:
        """Code fragments should default to self."""
        tagger = AuthorshipTagger()
        fragment = Fragment(
            title="Code snippet",
            source=FragmentSource(platform=SourcePlatform.CODE),
        )
        result = tagger.tag(fragment)
        assert result == Authorship.SELF


# ---------------------------------------------------------------------------
# AuthorshipTagger — Ambiguous/unknown platforms
# ---------------------------------------------------------------------------


class TestAuthorshipTaggerAmbiguous:
    """Tests for platforms that return None (ambiguous authorship)."""

    def test_email_is_none(self) -> None:
        """Email fragments should return None (ambiguous)."""
        tagger = AuthorshipTagger()
        fragment = Fragment(
            title="Email message",
            source=FragmentSource(platform=SourcePlatform.EMAIL),
        )
        result = tagger.tag(fragment)
        assert result is None

    def test_image_ocr_is_none(self) -> None:
        """Image OCR fragments should return None (ambiguous)."""
        tagger = AuthorshipTagger()
        fragment = Fragment(
            title="OCR text",
            source=FragmentSource(platform=SourcePlatform.IMAGE_OCR),
        )
        result = tagger.tag(fragment)
        assert result is None

    def test_other_platform_is_none(self) -> None:
        """Other/unknown platform fragments should return None."""
        tagger = AuthorshipTagger()
        fragment = Fragment(
            title="Unknown content",
            source=FragmentSource(platform=SourcePlatform.OTHER),
        )
        result = tagger.tag(fragment)
        assert result is None


# ---------------------------------------------------------------------------
# AuthorshipTagger — Edge cases
# ---------------------------------------------------------------------------


class TestAuthorshipTaggerEdgeCases:
    """Tests for edge cases in authorship tagging."""

    def test_tag_returns_authorship_or_none(self) -> None:
        """tag() should always return Authorship or None."""
        tagger = AuthorshipTagger()
        platforms = [
            SourcePlatform.CLAUDE,
            SourcePlatform.CHATGPT,
            SourcePlatform.DISCORD,
            SourcePlatform.JOURNAL,
            SourcePlatform.ESSAY,
            SourcePlatform.CODE,
            SourcePlatform.EMAIL,
            SourcePlatform.IMAGE_OCR,
            SourcePlatform.OTHER,
        ]
        for platform in platforms:
            fragment = Fragment(
                title="Test",
                source=FragmentSource(platform=platform),
            )
            result = tagger.tag(fragment)
            assert result is None or isinstance(result, Authorship), (
                f"Unexpected return type for platform {platform}: {type(result)}"
            )

    def test_discord_username_not_used_for_non_discord(self) -> None:
        """discord_username parameter should not affect non-Discord platforms."""
        tagger = AuthorshipTagger()
        fragment = Fragment(
            title="Journal entry",
            source=FragmentSource(platform=SourcePlatform.JOURNAL),
        )
        result = tagger.tag(fragment, discord_username="myuser")
        assert result == Authorship.SELF

    def test_claude_role_case_insensitive(self) -> None:
        """Claude role matching should be case-insensitive."""
        tagger = AuthorshipTagger()
        fragment = Fragment(
            title="Turn",
            source=FragmentSource(
                platform=SourcePlatform.CLAUDE,
                interlocutor="Human",
            ),
        )
        result = tagger.tag(fragment)
        assert result == Authorship.SELF

    def test_chatgpt_role_case_insensitive(self) -> None:
        """ChatGPT role matching should be case-insensitive."""
        tagger = AuthorshipTagger()
        fragment = Fragment(
            title="Turn",
            source=FragmentSource(
                platform=SourcePlatform.CHATGPT,
                interlocutor="Assistant",
            ),
        )
        result = tagger.tag(fragment)
        assert result == Authorship.AI

    def test_tagger_is_reusable(self) -> None:
        """Same tagger instance should work across multiple calls."""
        tagger = AuthorshipTagger()

        frag1 = Fragment(
            title="Journal",
            source=FragmentSource(platform=SourcePlatform.JOURNAL),
        )
        frag2 = Fragment(
            title="Claude",
            source=FragmentSource(
                platform=SourcePlatform.CLAUDE,
                interlocutor="assistant",
            ),
        )
        frag3 = Fragment(
            title="Discord",
            source=FragmentSource(
                platform=SourcePlatform.DISCORD,
                interlocutor="friend",
            ),
        )

        assert tagger.tag(frag1) == Authorship.SELF
        assert tagger.tag(frag2) == Authorship.AI
        assert tagger.tag(frag3, discord_username="me") == Authorship.OTHER

    def test_interlocutor_whitespace_stripped(self) -> None:
        """Interlocutor values with whitespace should be handled."""
        tagger = AuthorshipTagger()
        fragment = Fragment(
            title="Turn",
            source=FragmentSource(
                platform=SourcePlatform.CLAUDE,
                interlocutor="  human  ",
            ),
        )
        result = tagger.tag(fragment)
        assert result == Authorship.SELF
