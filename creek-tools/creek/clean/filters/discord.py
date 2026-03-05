"""Discord pre-ingestion filter — skip bots, emoji-only, commands, low-value messages.

Implements the :class:`DiscordFilter` which evaluates raw Discord message
dicts against configurable rules and returns a :class:`FilterResult`
indicating whether the message should be kept or skipped.

Filter rules (all configurable, all enabled by default):

- **Bot messages**: skip messages where the author is a bot.
- **Emoji-only / sticker-only**: skip messages that are exclusively
  Unicode emoji characters (with optional whitespace).
- **Command invocations**: skip messages starting with ``/``, ``!``,
  or ``.`` followed by an alphanumeric command word.
- **Media-only**: skip messages with attachments but no text content.
- **Minimum length**: skip messages shorter than a configurable
  threshold (default 3 characters after stripping).
- **Link dumps**: flag (but keep) messages that consist exclusively
  of URLs with no surrounding commentary.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from creek.clean.filters import FilterResult

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
"""Matches common Unicode emoji character ranges."""

_COMMAND_PATTERN: re.Pattern[str] = re.compile(
    r"^\s*[/!\.][a-zA-Z]\w*",
)
"""Matches command invocations starting with ``/``, ``!``, or ``.``
followed by an alphabetic character and optional word characters.
Ellipsis (``...``) is excluded because the first char after the dot
must be alphabetic."""

_URL_PATTERN: re.Pattern[str] = re.compile(
    r"https?://[^\s]+",
    re.IGNORECASE,
)
"""Matches HTTP/HTTPS URLs."""


# ---------------------------------------------------------------------------
# DiscordFilter
# ---------------------------------------------------------------------------


class DiscordFilter:
    """Pre-ingestion filter for Discord messages.

    Evaluates raw Discord message dicts against configurable rules and
    returns a :class:`FilterResult` for each message.  The filter does
    not modify the input message.

    All filter rules are enabled by default and can be individually
    toggled via constructor parameters.

    Attributes:
        skip_bots: Whether to filter bot messages.
        skip_emoji_only: Whether to filter emoji-only messages.
        skip_commands: Whether to filter command invocations.
        skip_media_only: Whether to filter attachment-only messages.
        skip_link_dumps: Whether to flag URL-only messages.
        min_length: Minimum content length after stripping whitespace.
    """

    def __init__(
        self,
        *,
        skip_bots: bool = True,
        skip_emoji_only: bool = True,
        skip_commands: bool = True,
        skip_media_only: bool = True,
        skip_link_dumps: bool = True,
        min_length: int = 3,
    ) -> None:
        """Initialise the Discord filter with configurable rules.

        Args:
            skip_bots: Filter messages from bot authors.
            skip_emoji_only: Filter messages that are exclusively emoji.
            skip_commands: Filter command invocations (``/``, ``!``, ``.``).
            skip_media_only: Filter messages with attachments but no text.
            skip_link_dumps: Flag messages that are exclusively URLs.
            min_length: Minimum character count after stripping; messages
                shorter than this are filtered.  Set to ``0`` to disable.
        """
        self.skip_bots = skip_bots
        self.skip_emoji_only = skip_emoji_only
        self.skip_commands = skip_commands
        self.skip_media_only = skip_media_only
        self.skip_link_dumps = skip_link_dumps
        self.min_length = min_length

    def apply(self, message: dict[str, Any]) -> FilterResult:
        """Apply all enabled filter rules to a Discord message.

        Rules are checked in priority order: bot, media-only, emoji-only,
        minimum length, command, and finally link-dump.  The first rule
        that triggers short-circuits evaluation.

        Args:
            message: A Discord message dict with at minimum ``content``
                and optionally ``author``, ``attachments`` keys.

        Returns:
            A :class:`FilterResult` indicating whether to keep or skip
            the message, with an explanatory reason string when filtered
            or flagged.
        """
        # 1. Bot check (highest priority)
        if self.skip_bots and self._is_bot(message):
            return FilterResult(keep=False, reason="Bot message")

        content = str(message.get("content", ""))
        stripped = content.strip()

        # 2. Media-only check (before length, since empty text is expected)
        if self.skip_media_only and self._is_media_only(message, stripped):
            return FilterResult(
                keep=False,
                reason="Media-only message (attachment without text)",
            )

        return self._apply_content_rules(stripped)

    def _apply_content_rules(self, stripped: str) -> FilterResult:
        """Apply content-based filter rules to stripped message text.

        Checks emoji-only, minimum length, command invocation, and
        link-dump rules in priority order.

        Args:
            stripped: Whitespace-stripped message content.

        Returns:
            A :class:`FilterResult` for the content checks.
        """
        # 3. Emoji-only check (before min_length for a more specific reason)
        if self.skip_emoji_only and self._is_emoji_only(stripped):
            return FilterResult(
                keep=False,
                reason="Emoji-only message",
            )

        # 4. Minimum length check
        if self.min_length > 0 and len(stripped) < self.min_length:
            return FilterResult(
                keep=False,
                reason=(
                    f"Below minimum length: {len(stripped)} chars"
                    f" (min {self.min_length})"
                ),
            )

        # 5. Command invocation check
        if self.skip_commands and self._is_command(stripped):
            return FilterResult(
                keep=False,
                reason="Command invocation",
            )

        # 6. Link dump check (flag, don't skip)
        if self.skip_link_dumps and self._is_link_dump(stripped):
            return FilterResult(
                keep=True,
                reason="Link dump (URL-only, no commentary)",
            )

        return FilterResult(keep=True)

    # ---- Individual rule checks ----

    @staticmethod
    def _is_bot(message: dict[str, Any]) -> bool:
        """Check whether the message author is a bot.

        Args:
            message: A Discord message dict.

        Returns:
            ``True`` if the author's ``isBot`` field is truthy.
        """
        author = message.get("author")
        if not isinstance(author, dict):
            return False
        return bool(author.get("isBot", False))

    @staticmethod
    def _is_emoji_only(stripped: str) -> bool:
        """Check whether content consists exclusively of emoji.

        Removes all Unicode emoji and whitespace; if nothing remains,
        the content is emoji-only.

        Args:
            stripped: Whitespace-stripped message content.

        Returns:
            ``True`` if the content is exclusively emoji characters.
        """
        if not stripped:
            return False
        without_emoji = _EMOJI_PATTERN.sub("", stripped).strip()
        return len(without_emoji) == 0

    @staticmethod
    def _is_command(stripped: str) -> bool:
        """Check whether content is a command invocation.

        Matches messages starting with ``/``, ``!``, or ``.`` followed
        by an alphabetic character.  This excludes ellipsis (``...``)
        and lone punctuation.

        Args:
            stripped: Whitespace-stripped message content.

        Returns:
            ``True`` if the content matches a command pattern.
        """
        return bool(_COMMAND_PATTERN.match(stripped))

    @staticmethod
    def _is_media_only(message: dict[str, Any], stripped: str) -> bool:
        """Check whether the message has attachments but no text content.

        Args:
            message: A Discord message dict.
            stripped: Whitespace-stripped message content.

        Returns:
            ``True`` if attachments are present and text content is empty.
        """
        attachments = message.get("attachments")
        if not isinstance(attachments, list) or not attachments:
            return False
        return len(stripped) == 0

    @staticmethod
    def _is_link_dump(stripped: str) -> bool:
        """Check whether content consists exclusively of URLs.

        Removes all URLs from the content; if only whitespace remains,
        the message is a link dump.

        Args:
            stripped: Whitespace-stripped message content.

        Returns:
            ``True`` if the content is exclusively URLs.
        """
        if not stripped:
            return False
        without_urls = _URL_PATTERN.sub("", stripped).strip()
        return len(without_urls) == 0
