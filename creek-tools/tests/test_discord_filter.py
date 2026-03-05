"""Tests for creek.clean.filters.discord — Discord pre-ingestion filter.

Tests cover:
- FilterResult model structure and fields
- DiscordFilter bot message detection
- DiscordFilter emoji-only / sticker-only message detection
- DiscordFilter command invocation detection (``/``, ``!``, ``.`` prefixes)
- DiscordFilter media-only detection (attachments with no text)
- DiscordFilter minimum length threshold
- DiscordFilter link-dump detection (URL-only with no commentary)
- Configurable thresholds via constructor parameters
- Disabling individual filter rules
- Edge cases: empty content, whitespace, mixed content, custom emoji text
- Reason string clarity and descriptiveness
"""

from __future__ import annotations

from typing import Any

from creek.clean.filters import FilterResult
from creek.clean.filters.discord import DiscordFilter

# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _make_msg(
    content: str = "Hello world",
    author_name: str = "Alice",
    is_bot: bool = False,
    attachments: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Create a Discord message dict for filter testing.

    Args:
        content: The message text content.
        author_name: The author display name.
        is_bot: Whether the author is a bot.
        attachments: Optional list of attachment dicts.

    Returns:
        A message dict matching Discord export format.
    """
    msg: dict[str, Any] = {
        "id": "msg-001",
        "author": {
            "id": "user-001",
            "name": author_name,
            "isBot": is_bot,
        },
        "content": content,
        "timestamp": "2024-11-10T14:00:00Z",
    }
    if attachments is not None:
        msg["attachments"] = attachments
    return msg


# ---------------------------------------------------------------------------
# FilterResult model
# ---------------------------------------------------------------------------


class TestFilterResult:
    """Tests for the FilterResult Pydantic model."""

    def test_keep_true_no_reason(self) -> None:
        """A kept message should have keep=True and reason=None by default."""
        result = FilterResult(keep=True)
        assert result.keep is True
        assert result.reason is None

    def test_keep_false_with_reason(self) -> None:
        """A filtered message should have keep=False and a reason string."""
        result = FilterResult(keep=False, reason="Bot message")
        assert result.keep is False
        assert result.reason == "Bot message"

    def test_keep_true_with_reason(self) -> None:
        """A kept message can optionally have a reason (e.g. flagged)."""
        result = FilterResult(keep=True, reason="Link dump flagged")
        assert result.keep is True
        assert result.reason == "Link dump flagged"


# ---------------------------------------------------------------------------
# DiscordFilter — Bot messages
# ---------------------------------------------------------------------------


class TestDiscordFilterBot:
    """Tests for bot message filtering."""

    def test_bot_message_filtered(self) -> None:
        """Messages from bot authors should be filtered out."""
        f = DiscordFilter()
        msg = _make_msg(content="Bot response here", is_bot=True)
        result = f.apply(msg)
        assert result.keep is False
        assert result.reason is not None
        assert "bot" in result.reason.lower()

    def test_non_bot_message_kept(self) -> None:
        """Messages from human authors should be kept."""
        f = DiscordFilter()
        msg = _make_msg(content="Hello everyone!", is_bot=False)
        result = f.apply(msg)
        assert result.keep is True

    def test_bot_filter_disabled(self) -> None:
        """Bot messages should be kept when skip_bots is disabled."""
        f = DiscordFilter(skip_bots=False)
        msg = _make_msg(content="Bot response", is_bot=True)
        result = f.apply(msg)
        assert result.keep is True

    def test_missing_is_bot_field_treated_as_human(self) -> None:
        """Messages without isBot field should be treated as human."""
        f = DiscordFilter()
        msg: dict[str, Any] = {
            "id": "msg-001",
            "author": {"id": "user-001", "name": "Alice"},
            "content": "Hello world",
            "timestamp": "2024-11-10T14:00:00Z",
        }
        result = f.apply(msg)
        assert result.keep is True

    def test_missing_author_dict_treated_as_human(self) -> None:
        """Messages without author dict should not crash and not filter as bot."""
        f = DiscordFilter()
        msg: dict[str, Any] = {
            "id": "msg-001",
            "content": "Hello world",
            "timestamp": "2024-11-10T14:00:00Z",
        }
        result = f.apply(msg)
        assert result.keep is True


# ---------------------------------------------------------------------------
# DiscordFilter — Emoji-only / sticker-only
# ---------------------------------------------------------------------------


class TestDiscordFilterEmoji:
    """Tests for emoji-only and sticker-only message filtering."""

    def test_single_emoji_filtered(self) -> None:
        """A message with only a single emoji should be filtered."""
        f = DiscordFilter()
        msg = _make_msg(content="\U0001f600")  # grinning face
        result = f.apply(msg)
        assert result.keep is False
        assert result.reason is not None
        assert "emoji" in result.reason.lower()

    def test_multiple_emoji_filtered(self) -> None:
        """A message with only multiple emoji should be filtered."""
        f = DiscordFilter()
        msg = _make_msg(content="\U0001f600\U0001f44d\U0001f389")
        result = f.apply(msg)
        assert result.keep is False

    def test_emoji_with_whitespace_filtered(self) -> None:
        """Emoji separated by whitespace should still be filtered."""
        f = DiscordFilter()
        msg = _make_msg(content="\U0001f600 \U0001f44d \U0001f389")
        result = f.apply(msg)
        assert result.keep is False

    def test_emoji_with_text_kept(self) -> None:
        """Messages with emoji AND meaningful text should be kept."""
        f = DiscordFilter()
        msg = _make_msg(content="Great job! \U0001f44d")
        result = f.apply(msg)
        assert result.keep is True

    def test_emoji_filter_disabled(self) -> None:
        """Emoji-only messages should be kept when skip_emoji_only is False."""
        f = DiscordFilter(skip_emoji_only=False, min_length=0)
        msg = _make_msg(content="\U0001f600")
        result = f.apply(msg)
        assert result.keep is True

    def test_discord_custom_emoji_text_not_filtered(self) -> None:
        """Discord custom emoji notation like :smile: should not be emoji-only."""
        f = DiscordFilter()
        msg = _make_msg(content=":custom_emoji:")
        result = f.apply(msg)
        # This is text, not actual emoji — should pass emoji check
        # (might still be filtered by min_length depending on threshold)
        assert result.reason is None or "emoji" not in result.reason.lower()


# ---------------------------------------------------------------------------
# DiscordFilter — Command invocations
# ---------------------------------------------------------------------------


class TestDiscordFilterCommands:
    """Tests for command invocation filtering."""

    def test_slash_command_filtered(self) -> None:
        """Messages starting with /command should be filtered."""
        f = DiscordFilter()
        msg = _make_msg(content="/play some song")
        result = f.apply(msg)
        assert result.keep is False
        assert result.reason is not None
        assert "command" in result.reason.lower()

    def test_bang_command_filtered(self) -> None:
        """Messages starting with !command should be filtered."""
        f = DiscordFilter()
        msg = _make_msg(content="!help")
        result = f.apply(msg)
        assert result.keep is False
        assert "command" in result.reason.lower()

    def test_dot_command_filtered(self) -> None:
        """Messages starting with .command should be filtered."""
        f = DiscordFilter()
        msg = _make_msg(content=".rank")
        result = f.apply(msg)
        assert result.keep is False
        assert "command" in result.reason.lower()

    def test_command_with_leading_whitespace(self) -> None:
        """Commands with leading whitespace should still be detected."""
        f = DiscordFilter()
        msg = _make_msg(content="  /play music")
        result = f.apply(msg)
        assert result.keep is False

    def test_slash_not_followed_by_word_kept(self) -> None:
        """A lone slash or slash not followed by a word should be kept."""
        f = DiscordFilter()
        msg = _make_msg(content="/ not a command at all really")
        result = f.apply(msg)
        # '/ ' is not a command pattern — slash must be followed by alphanumeric
        assert result.keep is True

    def test_normal_text_starting_with_dot_not_command(self) -> None:
        """A sentence starting with '...' (ellipsis) should not be a command."""
        f = DiscordFilter()
        msg = _make_msg(content="...anyway I was thinking about this")
        result = f.apply(msg)
        assert result.keep is True

    def test_command_filter_disabled(self) -> None:
        """Commands should be kept when skip_commands is disabled."""
        f = DiscordFilter(skip_commands=False)
        msg = _make_msg(content="/play music")
        result = f.apply(msg)
        assert result.keep is True

    def test_url_path_not_treated_as_command(self) -> None:
        """A URL with a path should not be treated as a slash command."""
        f = DiscordFilter(skip_link_dumps=False)
        msg = _make_msg(content="Check this https://example.com/page for info")
        result = f.apply(msg)
        assert result.keep is True


# ---------------------------------------------------------------------------
# DiscordFilter — Media-only
# ---------------------------------------------------------------------------


class TestDiscordFilterMedia:
    """Tests for media-only message filtering."""

    def test_attachment_no_text_filtered(self) -> None:
        """Messages with attachments but no text should be filtered."""
        f = DiscordFilter()
        msg = _make_msg(
            content="",
            attachments=[{"url": "https://cdn.discord.com/image.png"}],
        )
        result = f.apply(msg)
        assert result.keep is False
        assert result.reason is not None
        assert "media" in result.reason.lower() or "attachment" in result.reason.lower()

    def test_attachment_with_text_kept(self) -> None:
        """Messages with attachments AND text should be kept."""
        f = DiscordFilter()
        msg = _make_msg(
            content="Here is a screenshot of the bug",
            attachments=[{"url": "https://cdn.discord.com/image.png"}],
        )
        result = f.apply(msg)
        assert result.keep is True

    def test_attachment_whitespace_only_text_filtered(self) -> None:
        """Messages with attachments and only whitespace text should be filtered."""
        f = DiscordFilter()
        msg = _make_msg(
            content="   ",
            attachments=[{"url": "https://cdn.discord.com/image.png"}],
        )
        result = f.apply(msg)
        assert result.keep is False

    def test_media_filter_disabled(self) -> None:
        """Media-only messages should be kept when skip_media_only is disabled."""
        f = DiscordFilter(skip_media_only=False, min_length=0)
        msg = _make_msg(
            content="",
            attachments=[{"url": "https://cdn.discord.com/image.png"}],
        )
        result = f.apply(msg)
        assert result.keep is True

    def test_no_attachments_no_text_not_media_filtered(self) -> None:
        """Empty messages without attachments should not be media-filtered."""
        f = DiscordFilter()
        msg = _make_msg(content="")
        result = f.apply(msg)
        # Should be filtered by min_length, not media
        assert result.keep is False
        assert result.reason is not None
        assert "media" not in result.reason.lower()


# ---------------------------------------------------------------------------
# DiscordFilter — Minimum length
# ---------------------------------------------------------------------------


class TestDiscordFilterMinLength:
    """Tests for minimum length filtering."""

    def test_too_short_filtered(self) -> None:
        """Messages shorter than min_length should be filtered."""
        f = DiscordFilter()
        msg = _make_msg(content="hi")
        result = f.apply(msg)
        assert result.keep is False
        assert result.reason is not None
        assert "length" in result.reason.lower() or "short" in result.reason.lower()

    def test_exactly_min_length_kept(self) -> None:
        """Messages exactly at min_length should be kept."""
        f = DiscordFilter(min_length=3)
        msg = _make_msg(content="yes")
        result = f.apply(msg)
        assert result.keep is True

    def test_above_min_length_kept(self) -> None:
        """Messages above min_length should be kept."""
        f = DiscordFilter(min_length=3)
        msg = _make_msg(content="Hello everyone!")
        result = f.apply(msg)
        assert result.keep is True

    def test_empty_content_filtered(self) -> None:
        """Empty string content should be filtered."""
        f = DiscordFilter()
        msg = _make_msg(content="")
        result = f.apply(msg)
        assert result.keep is False

    def test_whitespace_only_filtered(self) -> None:
        """Whitespace-only content should be filtered by length check."""
        f = DiscordFilter()
        msg = _make_msg(content="   ")
        result = f.apply(msg)
        assert result.keep is False

    def test_custom_min_length(self) -> None:
        """Custom min_length threshold should be respected."""
        f = DiscordFilter(min_length=10)
        msg = _make_msg(content="Short")
        result = f.apply(msg)
        assert result.keep is False

    def test_min_length_zero_allows_short(self) -> None:
        """Setting min_length=0 should allow any length content."""
        f = DiscordFilter(min_length=0)
        msg = _make_msg(content="a")
        result = f.apply(msg)
        assert result.keep is True

    def test_min_length_disabled_via_zero(self) -> None:
        """Setting min_length=0 effectively disables the length filter."""
        f = DiscordFilter(min_length=0)
        msg = _make_msg(content="ok")
        result = f.apply(msg)
        assert result.keep is True


# ---------------------------------------------------------------------------
# DiscordFilter — Link dumps
# ---------------------------------------------------------------------------


class TestDiscordFilterLinkDumps:
    """Tests for link-dump detection (URL-only messages)."""

    def test_single_url_flagged(self) -> None:
        """A message that is a single URL should be flagged (keep=True with reason)."""
        f = DiscordFilter()
        msg = _make_msg(content="https://example.com/article")
        result = f.apply(msg)
        assert result.keep is True
        assert result.reason is not None
        assert "link" in result.reason.lower() or "url" in result.reason.lower()

    def test_multiple_urls_no_text_flagged(self) -> None:
        """Multiple URLs with no commentary should be flagged."""
        f = DiscordFilter()
        msg = _make_msg(content="https://example.com https://other.com")
        result = f.apply(msg)
        assert result.keep is True
        assert result.reason is not None

    def test_url_with_commentary_kept_clean(self) -> None:
        """URLs with surrounding commentary should be kept without flag."""
        f = DiscordFilter()
        msg = _make_msg(
            content="Check out this article: https://example.com/article it is great"
        )
        result = f.apply(msg)
        assert result.keep is True
        assert result.reason is None

    def test_link_dump_filter_disabled(self) -> None:
        """Link dumps should not be flagged when skip_link_dumps is False."""
        f = DiscordFilter(skip_link_dumps=False)
        msg = _make_msg(content="https://example.com/article")
        result = f.apply(msg)
        assert result.keep is True
        assert result.reason is None

    def test_http_url_flagged(self) -> None:
        """HTTP URLs (not just HTTPS) should also be detected."""
        f = DiscordFilter()
        msg = _make_msg(content="http://example.com/page")
        result = f.apply(msg)
        assert result.keep is True
        assert result.reason is not None


# ---------------------------------------------------------------------------
# DiscordFilter — Integration / ordering
# ---------------------------------------------------------------------------


class TestDiscordFilterIntegration:
    """Tests for filter rule ordering and combined behavior."""

    def test_normal_message_kept(self) -> None:
        """A normal human message should pass all filters."""
        f = DiscordFilter()
        msg = _make_msg(content="I think we should refactor the auth module")
        result = f.apply(msg)
        assert result.keep is True
        assert result.reason is None

    def test_bot_takes_precedence_over_length(self) -> None:
        """Bot filter should fire before length check."""
        f = DiscordFilter()
        msg = _make_msg(content="This is a long bot message with details", is_bot=True)
        result = f.apply(msg)
        assert result.keep is False
        assert "bot" in result.reason.lower()

    def test_all_filters_disabled(self) -> None:
        """With all filters disabled, any message should be kept."""
        f = DiscordFilter(
            skip_bots=False,
            skip_emoji_only=False,
            skip_commands=False,
            skip_media_only=False,
            skip_link_dumps=False,
            min_length=0,
        )
        msg = _make_msg(content="!help", is_bot=True)
        result = f.apply(msg)
        assert result.keep is True

    def test_default_min_length_is_three(self) -> None:
        """Default min_length should be 3."""
        f = DiscordFilter()
        # 2 chars should fail, 3 chars should pass
        msg_short = _make_msg(content="ab")
        msg_ok = _make_msg(content="abc")
        assert f.apply(msg_short).keep is False
        assert f.apply(msg_ok).keep is True

    def test_multiple_messages_batch(self) -> None:
        """Filter should work correctly on a batch of varied messages."""
        f = DiscordFilter()
        messages = [
            _make_msg(content="Normal discussion about architecture"),
            _make_msg(content="!roll 20", is_bot=False),
            _make_msg(content="\U0001f44d"),
            _make_msg(content="Bot said something", is_bot=True),
            _make_msg(content="hi"),
            _make_msg(content="https://example.com"),
        ]
        results = [f.apply(m) for m in messages]
        # Normal: kept
        assert results[0].keep is True
        assert results[0].reason is None
        # Command: filtered
        assert results[1].keep is False
        # Emoji: filtered
        assert results[2].keep is False
        # Bot: filtered
        assert results[3].keep is False
        # Too short: filtered
        assert results[4].keep is False
        # Link dump: flagged (kept but with reason)
        assert results[5].keep is True
        assert results[5].reason is not None

    def test_filter_does_not_modify_message(self) -> None:
        """The filter should not modify the input message dict."""
        f = DiscordFilter()
        msg = _make_msg(content="Hello everyone in the channel!")
        import copy

        original = copy.deepcopy(msg)
        f.apply(msg)
        assert msg == original

    def test_media_only_bot_gets_bot_reason(self) -> None:
        """A bot message with attachments and no text should cite bot as reason."""
        f = DiscordFilter()
        msg = _make_msg(
            content="",
            is_bot=True,
            attachments=[{"url": "https://cdn.discord.com/img.png"}],
        )
        result = f.apply(msg)
        assert result.keep is False
        # Bot check should fire first
        assert "bot" in result.reason.lower()
