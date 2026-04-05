"""Discord pre-ingestion filter — skip bots, emoji-only, commands, low-value.

Filters raw Discord message dicts **before** fragment creation to remove
noise: bot messages, emoji/sticker-only posts, command invocations,
media-only attachments, below-minimum-length text, and link dumps.

All rules are individually configurable via :class:`DiscordFilterConfig`
and enabled by default.  Contextually valuable short messages (replies)
are preserved even when they fall below the minimum length threshold.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Regex patterns
# ---------------------------------------------------------------------------

_EMOJI_PATTERN: re.Pattern[str] = re.compile(
    r"[\U0001f600-\U0001f64f"
    r"\U0001f300-\U0001f5ff"
    r"\U0001f680-\U0001f6ff"
    r"\U0001f1e0-\U0001f1ff"
    r"\U00002702-\U000027b0"
    r"\U0000fe00-\U0000fe0f"
    r"\U0001f900-\U0001f9ff"
    r"\U0001fa00-\U0001fa6f"
    r"\U0001fa70-\U0001faff"
    r"\U00002600-\U000026ff]+",
)
"""Matches Unicode emoji character ranges."""

_CUSTOM_EMOJI_PATTERN: re.Pattern[str] = re.compile(
    r"<a?:\w+:\d+>",
)
"""Matches Discord custom emoji syntax ``<:name:id>`` and ``<a:name:id>``."""

_URL_PATTERN: re.Pattern[str] = re.compile(
    r"https?://[^\s]+",
    re.IGNORECASE,
)
"""Matches HTTP/HTTPS URLs."""

_COMMAND_PATTERN: re.Pattern[str] = re.compile(
    r"^[/!.]\w+",
)
"""Matches command invocations at start of message (prefix + word)."""


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class FilterResult(BaseModel):
    """Result of filtering a single Discord message.

    Attributes:
        keep: Whether to keep the message (``True``) or skip it.
        reason: The filter rule that caused skipping, or ``None`` if kept.
    """

    keep: bool
    reason: str | None = None


class FilterStats(BaseModel):
    """Aggregate statistics from filtering a batch of messages.

    Attributes:
        total: Total number of messages evaluated.
        kept: Number of messages kept.
        skipped: Number of messages skipped.
        reasons: Mapping of skip-reason to count.
    """

    total: int = 0
    kept: int = 0
    skipped: int = 0
    reasons: dict[str, int] = Field(default_factory=dict)

    def record(self, result: FilterResult) -> None:
        """Record a single filter result into the running statistics.

        Args:
            result: The filter result to record.
        """
        self.total += 1
        if result.keep:
            self.kept += 1
        else:
            self.skipped += 1
            if result.reason:
                self.reasons[result.reason] = self.reasons.get(result.reason, 0) + 1


class DiscordFilterConfig(BaseModel):
    """Configuration for Discord pre-ingestion filtering.

    All filter rules are enabled by default.  Set individual flags to
    ``False`` to disable specific rules.

    Attributes:
        skip_bots: Skip messages where ``author.isBot`` is true.
        skip_emoji_only: Skip messages consisting exclusively of emoji.
        skip_commands: Skip messages starting with a command prefix.
        skip_media_only: Skip attachment-only messages lacking text.
        skip_below_min_length: Skip messages shorter than ``min_length``.
        flag_link_dumps: Skip messages that are exclusively URLs.
        min_length: Minimum content length in characters (default 3).
        command_prefixes: Characters that indicate a command invocation.
    """

    skip_bots: bool = True
    skip_emoji_only: bool = True
    skip_commands: bool = True
    skip_media_only: bool = True
    skip_below_min_length: bool = True
    flag_link_dumps: bool = True
    min_length: int = 3
    command_prefixes: list[str] = Field(default_factory=lambda: ["/", "!", "."])


# ---------------------------------------------------------------------------
# Filter
# ---------------------------------------------------------------------------


class DiscordFilter:
    """Pre-ingestion filter for Discord messages.

    Evaluates raw Discord message dicts against configurable filter rules
    and returns keep/skip decisions.  Contextually valuable short messages
    (replies) are preserved even when below minimum length.

    Attributes:
        config: The filter configuration.
    """

    def __init__(self, config: DiscordFilterConfig | None = None) -> None:
        """Initialise the filter with optional configuration.

        Args:
            config: Filter configuration.  Uses defaults if ``None``.
        """
        self.config = config or DiscordFilterConfig()

    def check(self, msg: dict[str, Any]) -> FilterResult:
        """Evaluate a single Discord message against all filter rules.

        Rules are applied in priority order:

        1. Bot messages
        2. Content-based checks (emoji, commands, media, length, links)

        Args:
            msg: A raw Discord message dict.

        Returns:
            A :class:`FilterResult` indicating keep or skip with reason.
        """
        if self.config.skip_bots and self._is_bot(msg):
            return FilterResult(keep=False, reason="bot_message")

        content = self._get_content(msg)
        return self._check_content(msg, content)

    def _check_content(
        self,
        msg: dict[str, Any],
        content: str,
    ) -> FilterResult:
        """Apply content-based filter rules to a message.

        Checks emoji-only, commands, and media-only first, then
        delegates length and link-dump checks.

        Args:
            msg: The raw Discord message dict.
            content: The extracted text content.

        Returns:
            A :class:`FilterResult` indicating keep or skip.
        """
        if self.config.skip_emoji_only and self._is_emoji_only(content):
            return FilterResult(keep=False, reason="emoji_only")

        if self.config.skip_commands and self._is_command(content):
            return FilterResult(keep=False, reason="command_invocation")

        if self.config.skip_media_only and self._is_media_only(msg, content):
            return FilterResult(keep=False, reason="media_only")

        return self._check_length_and_links(msg, content)

    def _check_length_and_links(
        self,
        msg: dict[str, Any],
        content: str,
    ) -> FilterResult:
        """Apply minimum-length and link-dump checks.

        Replies are exempted from the minimum length filter.

        Args:
            msg: The raw Discord message dict.
            content: The extracted text content.

        Returns:
            A :class:`FilterResult` indicating keep or skip.
        """
        if (
            self.config.skip_below_min_length
            and not self._is_reply(msg)
            and self._is_below_min_length(content)
        ):
            return FilterResult(keep=False, reason="below_min_length")

        if self.config.flag_link_dumps and self._is_link_dump(content):
            return FilterResult(keep=False, reason="link_dump")

        return FilterResult(keep=True, reason=None)

    def filter_messages(
        self,
        messages: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], FilterStats]:
        """Filter a batch of Discord messages and collect statistics.

        Args:
            messages: List of raw Discord message dicts.

        Returns:
            A tuple of (kept_messages, statistics).
        """
        stats = FilterStats()
        kept: list[dict[str, Any]] = []

        for msg in messages:
            result = self.check(msg)
            stats.record(result)
            if result.keep:
                kept.append(msg)

        self._log_stats(stats)
        return kept, stats

    # ---- Private check methods ----

    def _is_bot(self, msg: dict[str, Any]) -> bool:
        """Check if the message author is a bot.

        Args:
            msg: A Discord message dict.

        Returns:
            ``True`` if the author is flagged as a bot.
        """
        author = msg.get("author")
        if isinstance(author, dict):
            return bool(author.get("isBot", False))
        return False

    def _get_content(self, msg: dict[str, Any]) -> str:
        """Extract text content from a message dict.

        Args:
            msg: A Discord message dict.

        Returns:
            The message content string, or empty string if missing.
        """
        content = msg.get("content")
        if content is None:
            return ""
        return str(content)

    def _is_emoji_only(self, content: str) -> bool:
        """Check if content consists exclusively of emoji.

        Handles both Unicode emoji and Discord custom emoji syntax.

        Args:
            content: The message text content.

        Returns:
            ``True`` if the content is exclusively emoji/whitespace.
        """
        stripped = content.strip()
        if not stripped:
            return False

        # Remove Unicode emoji
        without_emoji = _EMOJI_PATTERN.sub("", stripped)
        # Remove Discord custom emoji
        without_emoji = _CUSTOM_EMOJI_PATTERN.sub("", without_emoji)
        # Check if anything remains after removing emoji and whitespace
        return not without_emoji.strip()

    def _is_command(self, content: str) -> bool:
        """Check if content is a command invocation.

        A command is defined as a message starting with one of the
        configured prefixes followed by a word character.

        Args:
            content: The message text content.

        Returns:
            ``True`` if the content matches a command pattern.
        """
        stripped = content.strip()
        if not stripped:
            return False

        for prefix in self.config.command_prefixes:
            if stripped.startswith(prefix) and len(stripped) > len(prefix):
                rest = stripped[len(prefix) :]
                if rest and rest[0].isalnum():
                    return True
        return False

    def _is_media_only(self, msg: dict[str, Any], content: str) -> bool:
        """Check if message has attachments but no text content.

        Args:
            msg: A Discord message dict.
            content: The extracted message text.

        Returns:
            ``True`` if the message has attachments but no text.
        """
        attachments = msg.get("attachments")
        if not isinstance(attachments, list) or not attachments:
            return False
        return not content.strip()

    def _is_reply(self, msg: dict[str, Any]) -> bool:
        """Check if the message is a reply to another message.

        Args:
            msg: A Discord message dict.

        Returns:
            ``True`` if the message has a reference to a parent message.
        """
        ref = msg.get("reference")
        return isinstance(ref, dict) and "messageId" in ref

    def _is_below_min_length(self, content: str) -> bool:
        """Check if content is below the configured minimum length.

        Args:
            content: The message text content.

        Returns:
            ``True`` if the stripped content length is below threshold.
        """
        return len(content.strip()) < self.config.min_length

    def _is_link_dump(self, content: str) -> bool:
        """Check if content consists exclusively of URLs.

        Args:
            content: The message text content.

        Returns:
            ``True`` if after removing URLs only whitespace remains.
        """
        stripped = content.strip()
        if not stripped:
            return False

        without_urls = _URL_PATTERN.sub("", stripped).strip()
        return not without_urls

    def _log_stats(self, stats: FilterStats) -> None:
        """Log filter statistics at INFO level.

        Args:
            stats: The accumulated filter statistics.
        """
        if stats.total == 0:
            return

        logger.info(
            "Discord filter: %d total, %d kept, %d skipped",
            stats.total,
            stats.kept,
            stats.skipped,
        )
        for reason, count in sorted(stats.reasons.items()):
            logger.info("  Skipped %d messages: %s", count, reason)
