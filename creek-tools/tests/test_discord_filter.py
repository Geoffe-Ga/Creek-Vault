"""Tests for creek.clean.filters.discord — Discord pre-ingestion filter.

Covers all filter rules: bot messages, emoji-only, command invocations,
media-only, minimum length, link dumps. Also tests configurability,
logging of filter statistics, and preservation of contextually valuable
short messages (e.g., replies).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from creek.clean.filters._result import FilterResult
from creek.clean.filters.discord import (
    DiscordFilter,
    DiscordFilterConfig,
    FilterStats,
)

if TYPE_CHECKING:
    import pytest


# ---- Fixture helpers ----


def _make_msg(
    msg_id: str = "msg-001",
    author: str = "Alice",
    content: str = "Hello world",
    timestamp: str = "2024-11-10T14:00:00Z",
    is_bot: bool = False,
    reference_id: str | None = None,
    attachments: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Create a Discord message dict for testing.

    Args:
        msg_id: The message ID.
        author: The author display name.
        content: The message text content.
        timestamp: ISO 8601 timestamp string.
        is_bot: Whether the author is a bot.
        reference_id: Optional ID of the replied-to message.
        attachments: Optional list of attachment dicts.

    Returns:
        A message dict matching Discord export format.
    """
    msg: dict[str, Any] = {
        "id": msg_id,
        "author": {"id": f"user-{author.lower()}", "name": author, "isBot": is_bot},
        "content": content,
        "timestamp": timestamp,
    }
    if reference_id is not None:
        msg["reference"] = {"messageId": reference_id}
    if attachments is not None:
        msg["attachments"] = attachments
    return msg


# ---------------------------------------------------------------------------
# FilterResult model
# ---------------------------------------------------------------------------


class TestFilterResult:
    """Tests for the FilterResult model."""

    def test_keep_result(self) -> None:
        """FilterResult should represent a kept message."""
        result = FilterResult(keep=True, reason=None)
        assert result.keep is True
        assert result.reason is None

    def test_skip_result_with_reason(self) -> None:
        """FilterResult should represent a skipped message with reason."""
        result = FilterResult(keep=False, reason="bot_message")
        assert result.keep is False
        assert result.reason == "bot_message"


# ---------------------------------------------------------------------------
# FilterStats model
# ---------------------------------------------------------------------------


class TestFilterStats:
    """Tests for the FilterStats model."""

    def test_default_stats(self) -> None:
        """FilterStats should initialise with zero counts."""
        stats = FilterStats()
        assert stats.total == 0
        assert stats.kept == 0
        assert stats.skipped == 0
        assert stats.reasons == {}

    def test_stats_record_skip(self) -> None:
        """FilterStats.record should update counts for skipped messages."""
        stats = FilterStats()
        stats.record(FilterResult(keep=False, reason="bot_message"))
        assert stats.total == 1
        assert stats.kept == 0
        assert stats.skipped == 1
        assert stats.reasons["bot_message"] == 1

    def test_stats_record_keep(self) -> None:
        """FilterStats.record should update counts for kept messages."""
        stats = FilterStats()
        stats.record(FilterResult(keep=True, reason=None))
        assert stats.total == 1
        assert stats.kept == 1
        assert stats.skipped == 0

    def test_stats_accumulate_reasons(self) -> None:
        """FilterStats should accumulate multiple reasons."""
        stats = FilterStats()
        stats.record(FilterResult(keep=False, reason="bot_message"))
        stats.record(FilterResult(keep=False, reason="bot_message"))
        stats.record(FilterResult(keep=False, reason="emoji_only"))
        assert stats.reasons["bot_message"] == 2
        assert stats.reasons["emoji_only"] == 1


# ---------------------------------------------------------------------------
# DiscordFilterConfig
# ---------------------------------------------------------------------------


class TestDiscordFilterConfig:
    """Tests for DiscordFilterConfig defaults and overrides."""

    def test_default_config(self) -> None:
        """Default config should have all filters enabled."""
        cfg = DiscordFilterConfig()
        assert cfg.skip_bots is True
        assert cfg.skip_emoji_only is True
        assert cfg.skip_commands is True
        assert cfg.skip_media_only is True
        assert cfg.skip_below_min_length is True
        assert cfg.flag_link_dumps is True
        assert cfg.min_length == 3

    def test_custom_config(self) -> None:
        """Config should accept custom values."""
        cfg = DiscordFilterConfig(skip_bots=False, min_length=10)
        assert cfg.skip_bots is False
        assert cfg.min_length == 10

    def test_command_prefixes_default(self) -> None:
        """Default command prefixes should include /, !, and . ."""
        cfg = DiscordFilterConfig()
        assert "/" in cfg.command_prefixes
        assert "!" in cfg.command_prefixes
        assert "." in cfg.command_prefixes


# ---------------------------------------------------------------------------
# DiscordFilter — Bot messages
# ---------------------------------------------------------------------------


class TestFilterBotMessages:
    """Tests for bot message filtering."""

    def test_skip_bot_message(self) -> None:
        """Bot messages should be skipped when filter is enabled."""
        f = DiscordFilter()
        msg = _make_msg(author="MEE6", is_bot=True, content="Level up!")
        result = f.check(msg)
        assert result.keep is False
        assert result.reason == "bot_message"

    def test_keep_bot_when_disabled(self) -> None:
        """Bot messages should be kept when filter is disabled."""
        cfg = DiscordFilterConfig(skip_bots=False)
        f = DiscordFilter(config=cfg)
        msg = _make_msg(author="MEE6", is_bot=True, content="Level up!")
        result = f.check(msg)
        assert result.keep is True

    def test_keep_non_bot_message(self) -> None:
        """Non-bot messages should pass the bot filter."""
        f = DiscordFilter()
        msg = _make_msg(author="Alice", is_bot=False, content="Hey there!")
        result = f.check(msg)
        assert result.keep is True

    def test_missing_is_bot_field(self) -> None:
        """Messages without isBot field should not be filtered as bots."""
        f = DiscordFilter()
        msg: dict[str, Any] = {
            "id": "1",
            "author": {"name": "Alice"},
            "content": "Hello there",
            "timestamp": "2024-01-01T00:00:00Z",
        }
        result = f.check(msg)
        assert result.keep is True


# ---------------------------------------------------------------------------
# DiscordFilter — Emoji-only
# ---------------------------------------------------------------------------


class TestFilterEmojiOnly:
    """Tests for emoji-only / sticker-only filtering."""

    def test_skip_emoji_only(self) -> None:
        """Messages with only emoji should be skipped."""
        f = DiscordFilter()
        msg = _make_msg(content="\U0001f600\U0001f602\U0001f60d")
        result = f.check(msg)
        assert result.keep is False
        assert result.reason == "emoji_only"

    def test_skip_emoji_with_whitespace(self) -> None:
        """Emoji messages with whitespace should be skipped."""
        f = DiscordFilter()
        msg = _make_msg(content=" \U0001f600 \U0001f602 ")
        result = f.check(msg)
        assert result.keep is False
        assert result.reason == "emoji_only"

    def test_keep_emoji_with_text(self) -> None:
        """Messages with emoji AND text should be kept."""
        f = DiscordFilter()
        msg = _make_msg(content="Great job! \U0001f600")
        result = f.check(msg)
        assert result.keep is True

    def test_keep_emoji_when_disabled(self) -> None:
        """Emoji-only messages should be kept when filter is disabled."""
        cfg = DiscordFilterConfig(skip_emoji_only=False, skip_below_min_length=False)
        f = DiscordFilter(config=cfg)
        msg = _make_msg(content="\U0001f600")
        result = f.check(msg)
        assert result.keep is True

    def test_skip_discord_custom_emoji(self) -> None:
        """Custom Discord emoji syntax <:name:id> should be treated as emoji."""
        f = DiscordFilter()
        msg = _make_msg(content="<:pogchamp:123456>")
        result = f.check(msg)
        assert result.keep is False
        assert result.reason == "emoji_only"

    def test_skip_animated_custom_emoji(self) -> None:
        """Animated Discord emoji <a:name:id> should be treated as emoji."""
        f = DiscordFilter()
        msg = _make_msg(content="<a:dance:789012>")
        result = f.check(msg)
        assert result.keep is False
        assert result.reason == "emoji_only"


# ---------------------------------------------------------------------------
# DiscordFilter — Command invocations
# ---------------------------------------------------------------------------


class TestFilterCommands:
    """Tests for command invocation filtering."""

    def test_skip_slash_command(self) -> None:
        """Messages starting with /command should be skipped."""
        f = DiscordFilter()
        msg = _make_msg(content="/play never gonna give you up")
        result = f.check(msg)
        assert result.keep is False
        assert result.reason == "command_invocation"

    def test_skip_bang_command(self) -> None:
        """Messages starting with !command should be skipped."""
        f = DiscordFilter()
        msg = _make_msg(content="!help")
        result = f.check(msg)
        assert result.keep is False
        assert result.reason == "command_invocation"

    def test_skip_dot_command(self) -> None:
        """Messages starting with .command should be skipped."""
        f = DiscordFilter()
        msg = _make_msg(content=".rank")
        result = f.check(msg)
        assert result.keep is False
        assert result.reason == "command_invocation"

    def test_keep_sentence_with_slash(self) -> None:
        """Sentences that happen to contain / should not be filtered."""
        f = DiscordFilter()
        msg = _make_msg(content="I think we should try a/b testing")
        result = f.check(msg)
        assert result.keep is True

    def test_keep_commands_when_disabled(self) -> None:
        """Commands should be kept when filter is disabled."""
        cfg = DiscordFilterConfig(skip_commands=False)
        f = DiscordFilter(config=cfg)
        msg = _make_msg(content="/play something")
        result = f.check(msg)
        assert result.keep is True

    def test_skip_with_custom_prefix(self) -> None:
        """Custom command prefixes should be recognised."""
        cfg = DiscordFilterConfig(command_prefixes=["?", "!"])
        f = DiscordFilter(config=cfg)
        msg = _make_msg(content="?help")
        result = f.check(msg)
        assert result.keep is False

    def test_keep_bare_prefix(self) -> None:
        """A bare prefix character without a command word should be kept."""
        f = DiscordFilter()
        msg = _make_msg(content="!")
        # Too short to be a real command — will be caught by min_length
        # but the command filter itself should not flag it
        result = f.check(msg)
        # Either skipped for min_length or kept — not command_invocation
        assert result.reason != "command_invocation" or result.keep is True


# ---------------------------------------------------------------------------
# DiscordFilter — Media-only
# ---------------------------------------------------------------------------


class TestFilterMediaOnly:
    """Tests for media-only message filtering."""

    def test_skip_attachment_no_text(self) -> None:
        """Messages with attachments but empty content should be skipped."""
        f = DiscordFilter()
        msg = _make_msg(
            content="",
            attachments=[
                {"url": "https://cdn.discord.com/img.png", "filename": "img.png"},
            ],
        )
        result = f.check(msg)
        assert result.keep is False
        assert result.reason == "media_only"

    def test_keep_attachment_with_text(self) -> None:
        """Messages with attachments AND text should be kept."""
        f = DiscordFilter()
        msg = _make_msg(
            content="Check out this picture!",
            attachments=[
                {"url": "https://cdn.discord.com/img.png", "filename": "img.png"},
            ],
        )
        result = f.check(msg)
        assert result.keep is True

    def test_keep_no_attachments_no_text(self) -> None:
        """Messages without attachments but empty content skip via min_length."""
        f = DiscordFilter()
        msg = _make_msg(content="")
        result = f.check(msg)
        # Should be skipped for min_length, not media_only
        assert result.reason != "media_only"

    def test_keep_media_when_disabled(self) -> None:
        """Media-only messages should be kept when filter is disabled."""
        cfg = DiscordFilterConfig(skip_media_only=False)
        f = DiscordFilter(config=cfg)
        msg = _make_msg(
            content="",
            attachments=[
                {"url": "https://cdn.discord.com/img.png", "filename": "img.png"},
            ],
        )
        result = f.check(msg)
        # Still may be caught by min_length, but not media_only
        assert result.reason != "media_only"


# ---------------------------------------------------------------------------
# DiscordFilter — Minimum length
# ---------------------------------------------------------------------------


class TestFilterMinLength:
    """Tests for minimum content length filtering."""

    def test_skip_very_short(self) -> None:
        """Messages shorter than min_length should be skipped."""
        f = DiscordFilter()
        msg = _make_msg(content="hi")
        result = f.check(msg)
        assert result.keep is False
        assert result.reason == "below_min_length"

    def test_keep_at_min_length(self) -> None:
        """Messages at exactly min_length should be kept."""
        f = DiscordFilter()
        msg = _make_msg(content="hey")
        result = f.check(msg)
        assert result.keep is True

    def test_skip_empty_content(self) -> None:
        """Empty content should be skipped for min_length."""
        f = DiscordFilter()
        msg = _make_msg(content="")
        result = f.check(msg)
        assert result.keep is False
        assert result.reason == "below_min_length"

    def test_keep_short_when_disabled(self) -> None:
        """Short messages should be kept when filter is disabled."""
        cfg = DiscordFilterConfig(skip_below_min_length=False)
        f = DiscordFilter(config=cfg)
        msg = _make_msg(content="hi")
        result = f.check(msg)
        assert result.keep is True

    def test_custom_min_length(self) -> None:
        """Custom min_length should be respected."""
        cfg = DiscordFilterConfig(min_length=10)
        f = DiscordFilter(config=cfg)
        msg = _make_msg(content="short")
        result = f.check(msg)
        assert result.keep is False
        assert result.reason == "below_min_length"

    def test_whitespace_only_counted_as_empty(self) -> None:
        """Whitespace-only messages should be skipped."""
        f = DiscordFilter()
        msg = _make_msg(content="   ")
        result = f.check(msg)
        assert result.keep is False
        assert result.reason == "below_min_length"


# ---------------------------------------------------------------------------
# DiscordFilter — Link dumps
# ---------------------------------------------------------------------------


class TestFilterLinkDumps:
    """Tests for link-dump flagging."""

    def test_flag_url_only(self) -> None:
        """Messages with only URLs should be flagged."""
        f = DiscordFilter()
        msg = _make_msg(content="https://example.com https://other.com")
        result = f.check(msg)
        assert result.keep is False
        assert result.reason == "link_dump"

    def test_keep_url_with_commentary(self) -> None:
        """URLs with surrounding commentary should be kept."""
        f = DiscordFilter()
        msg = _make_msg(content="Check this out: https://example.com really cool stuff")
        result = f.check(msg)
        assert result.keep is True

    def test_keep_link_dump_when_disabled(self) -> None:
        """Link dumps should be kept when flagging is disabled."""
        cfg = DiscordFilterConfig(flag_link_dumps=False)
        f = DiscordFilter(config=cfg)
        msg = _make_msg(content="https://example.com")
        result = f.check(msg)
        assert result.keep is True

    def test_single_url_no_text(self) -> None:
        """A single URL with no other text is a link dump."""
        f = DiscordFilter()
        msg = _make_msg(content="https://example.com")
        result = f.check(msg)
        assert result.keep is False
        assert result.reason == "link_dump"


# ---------------------------------------------------------------------------
# DiscordFilter — Contextually valuable short messages (replies)
# ---------------------------------------------------------------------------


class TestPreserveReplies:
    """Tests that contextually valuable short messages (replies) are preserved."""

    def test_keep_short_reply(self) -> None:
        """Short messages that are replies should be preserved."""
        f = DiscordFilter()
        msg = _make_msg(content="ok", reference_id="msg-parent")
        result = f.check(msg)
        assert result.keep is True

    def test_keep_single_char_reply(self) -> None:
        """Even single-char replies should be preserved."""
        f = DiscordFilter()
        msg = _make_msg(content="y", reference_id="msg-parent")
        result = f.check(msg)
        assert result.keep is True


# ---------------------------------------------------------------------------
# DiscordFilter — filter_messages batch method
# ---------------------------------------------------------------------------


class TestFilterMessages:
    """Tests for the batch filter_messages method."""

    def test_filter_batch(self) -> None:
        """filter_messages should return only kept messages."""
        f = DiscordFilter()
        messages = [
            _make_msg(msg_id="1", content="Hello everyone!"),
            _make_msg(msg_id="2", author="MEE6", is_bot=True, content="Level up!"),
            _make_msg(msg_id="3", content="\U0001f600"),
            _make_msg(msg_id="4", content="This is a real message"),
        ]
        kept, stats = f.filter_messages(messages)
        assert len(kept) == 2
        assert stats.total == 4
        assert stats.kept == 2
        assert stats.skipped == 2

    def test_filter_empty_list(self) -> None:
        """filter_messages should handle empty input."""
        f = DiscordFilter()
        kept, stats = f.filter_messages([])
        assert kept == []
        assert stats.total == 0

    def test_filter_all_kept(self) -> None:
        """filter_messages should keep all valid messages."""
        f = DiscordFilter()
        messages = [
            _make_msg(msg_id="1", content="First message here"),
            _make_msg(msg_id="2", content="Second message here"),
        ]
        kept, stats = f.filter_messages(messages)
        assert len(kept) == 2
        assert stats.skipped == 0

    def test_filter_all_skipped(self) -> None:
        """filter_messages should skip all invalid messages."""
        f = DiscordFilter()
        messages = [
            _make_msg(msg_id="1", author="Bot1", is_bot=True, content="ping"),
            _make_msg(msg_id="2", author="Bot2", is_bot=True, content="pong"),
        ]
        kept, stats = f.filter_messages(messages)
        assert len(kept) == 0
        assert stats.skipped == 2


# ---------------------------------------------------------------------------
# DiscordFilter — Logging
# ---------------------------------------------------------------------------


class TestFilterLogging:
    """Tests for filter statistics logging."""

    def test_stats_logged(self, caplog: pytest.LogCaptureFixture) -> None:
        """filter_messages should log filter statistics."""
        import logging

        f = DiscordFilter()
        messages = [
            _make_msg(msg_id="1", content="Keep this one"),
            _make_msg(msg_id="2", author="Bot", is_bot=True, content="Skip"),
        ]
        with caplog.at_level(logging.INFO, logger="creek.clean.filters.discord"):
            f.filter_messages(messages)
        assert any("skipped" in rec.message.lower() for rec in caplog.records)

    def test_no_log_when_nothing_filtered(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """No skip log when all messages are kept."""
        import logging

        f = DiscordFilter()
        messages = [_make_msg(content="A valid message here")]
        with caplog.at_level(logging.INFO, logger="creek.clean.filters.discord"):
            f.filter_messages(messages)
        # Should still log total, but no skip reasons
        skip_logs = [r for r in caplog.records if "bot_message" in r.message.lower()]
        assert skip_logs == []


# ---------------------------------------------------------------------------
# DiscordFilter — Edge cases
# ---------------------------------------------------------------------------


class TestFilterEdgeCases:
    """Tests for edge cases and robustness."""

    def test_missing_content_key(self) -> None:
        """Messages without a content key should be handled gracefully."""
        f = DiscordFilter()
        msg: dict[str, Any] = {
            "id": "1",
            "author": {"name": "Alice"},
            "timestamp": "2024-01-01T00:00:00Z",
        }
        result = f.check(msg)
        assert result.keep is False

    def test_missing_author_key(self) -> None:
        """Messages without an author key should be handled gracefully."""
        f = DiscordFilter()
        msg: dict[str, Any] = {
            "id": "1",
            "content": "Some text here",
            "timestamp": "2024-01-01T00:00:00Z",
        }
        result = f.check(msg)
        assert result.keep is True

    def test_none_content(self) -> None:
        """None content should be handled like empty."""
        f = DiscordFilter()
        msg = _make_msg(content="")
        msg["content"] = None
        result = f.check(msg)
        assert result.keep is False

    def test_filter_priority_bot_before_length(self) -> None:
        """Bot filter should trigger before length filter."""
        f = DiscordFilter()
        msg = _make_msg(author="Bot", is_bot=True, content="x")
        result = f.check(msg)
        assert result.reason == "bot_message"

    def test_mixed_emoji_and_custom_emoji(self) -> None:
        """Mixed Unicode and custom Discord emoji should be emoji_only."""
        f = DiscordFilter()
        msg = _make_msg(content="\U0001f600 <:custom:123>")
        result = f.check(msg)
        assert result.keep is False
        assert result.reason == "emoji_only"
