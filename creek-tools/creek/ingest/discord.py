"""Discord message ingestor — data package to fragments with context.

This module implements the ``DiscordIngestor`` class, which processes Discord
data exports (per-channel JSON files) into Creek fragments. Messages are
grouped by conversational context: reply chains and time-proximity blocks
(same author within 5 minutes). Channel metadata provides context headers.

Discord data package structure::

    messages/{channel_id}/messages.json — message array
    messages/{channel_id}/channel.json — channel metadata

Each message object::

    {
        "id": "...",
        "timestamp": "ISO8601",
        "content": "...",
        "author": {"name": "..."},
        "reference": {"messageId": "..."}
    }
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

from creek.ingest.base import (
    Ingestor,
    ParsedFragment,
    RawDocument,
    normalize_timestamp,
)

logger = logging.getLogger(__name__)

# ---- Constants ----

TIME_PROXIMITY_MINUTES = 5
"""Maximum gap (minutes) between messages from the same author to group."""

_SPOILER_PATTERN = re.compile(r"\|\|(.+?)\|\|")
"""Regex pattern matching Discord spoiler tags ``||content||``."""


# ---- Helper data structures ----


class _MessageGroup:
    """A group of temporally or reply-linked Discord messages.

    Attributes:
        channel_name: The name of the source Discord channel.
        messages: Ordered list of message dicts in this group.
        channel_id: The Discord channel ID string.
    """

    def __init__(
        self,
        channel_name: str,
        messages: list[dict[str, Any]],
        channel_id: str,
    ) -> None:
        """Initialise a message group.

        Args:
            channel_name: The display name of the channel.
            messages: The list of message dicts in this group.
            channel_id: The Discord channel ID string.
        """
        self.channel_name = channel_name
        self.messages = messages
        self.channel_id = channel_id


# ---- Discord formatting helpers ----


def _format_discord_content(content: str) -> str:
    """Convert Discord-specific formatting to Markdown equivalents.

    Handles spoiler tags by converting ``||text||`` to ``>!text!<`` style,
    and preserves standard Markdown formatting that Discord shares
    (bold, italic, code blocks, etc.).

    Args:
        content: The raw Discord message content string.

    Returns:
        The content with Discord formatting converted to Markdown.
    """
    # Convert spoiler tags: ||spoiler|| -> [SPOILER: spoiler]
    result = _SPOILER_PATTERN.sub(r"[SPOILER: \1]", content)
    return result


def _format_reply_context(parent_msg: dict[str, Any]) -> str:
    """Format a parent message as a quoted reply context block.

    Args:
        parent_msg: The parent message dict being replied to.

    Returns:
        A Markdown-formatted quote block with author attribution.
    """
    author = _safe_author_name(parent_msg)
    content = parent_msg.get("content", "")
    return f"> **{author}**: {content}"


def _safe_author_name(msg: dict[str, Any]) -> str:
    """Safely extract the author name from a message dict.

    Args:
        msg: A Discord message dict.

    Returns:
        The author name, or ``"Unknown"`` if missing.
    """
    author = msg.get("author")
    if isinstance(author, dict):
        return str(author.get("name", "Unknown"))
    return "Unknown"


def _safe_timestamp(msg: dict[str, Any]) -> str:
    """Safely extract the timestamp string from a message dict.

    Args:
        msg: A Discord message dict.

    Returns:
        The ISO 8601 timestamp string, or an empty string if missing.
    """
    return str(msg.get("timestamp", ""))


def _parse_msg_timestamp(msg: dict[str, Any]) -> datetime | None:
    """Parse the timestamp from a message dict into a datetime.

    Args:
        msg: A Discord message dict.

    Returns:
        A timezone-aware datetime, or ``None`` if parsing fails.
    """
    ts_str = _safe_timestamp(msg)
    if not ts_str:
        return None
    try:
        return datetime.fromisoformat(ts_str)
    except ValueError:
        return None


# ---- Grouping logic ----


def _build_message_index(messages: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Build a lookup index from message ID to message dict.

    Args:
        messages: The list of message dicts.

    Returns:
        A dict mapping message ID strings to their message dicts.
    """
    return {str(msg.get("id", "")): msg for msg in messages if msg.get("id")}


def _get_reference_id(msg: dict[str, Any]) -> str | None:
    """Extract the referenced (replied-to) message ID, if present.

    Args:
        msg: A Discord message dict.

    Returns:
        The referenced message ID string, or ``None``.
    """
    ref = msg.get("reference")
    if isinstance(ref, dict):
        mid = ref.get("messageId")
        if mid is not None:
            return str(mid)
    return None


def _group_messages(
    messages: list[dict[str, Any]],
) -> list[list[dict[str, Any]]]:
    """Group messages by reply chains and time proximity.

    Messages are processed in chronological order. A message joins the
    current group if:

    1. It is a reply to a message already in the current group, OR
    2. It is from the same author as the last message and within
       5 minutes (time proximity).

    Otherwise, a new group is started.

    Args:
        messages: A chronologically sorted list of message dicts.

    Returns:
        A list of message groups, each group being a list of message dicts.
    """
    if not messages:
        return []

    groups: list[list[dict[str, Any]]] = []
    current_group: list[dict[str, Any]] = [messages[0]]
    current_group_ids: set[str] = {str(messages[0].get("id", ""))}

    for msg in messages[1:]:
        if _should_join_group(msg, current_group, current_group_ids):
            current_group.append(msg)
            msg_id = str(msg.get("id", ""))
            if msg_id:
                current_group_ids.add(msg_id)
        else:
            groups.append(current_group)
            current_group = [msg]
            current_group_ids = {str(msg.get("id", ""))}

    groups.append(current_group)
    return groups


def _should_join_group(
    msg: dict[str, Any],
    current_group: list[dict[str, Any]],
    current_group_ids: set[str],
) -> bool:
    """Determine whether a message should join the current group.

    Args:
        msg: The message to evaluate.
        current_group: The current group of messages.
        current_group_ids: Set of message IDs in the current group.

    Returns:
        ``True`` if the message should join the current group.
    """
    # Check reply chain: does this message reply to one in the group?
    ref_id = _get_reference_id(msg)
    if ref_id is not None and ref_id in current_group_ids:
        return True

    # Check time proximity: same author within 5 minutes of last message
    last_msg = current_group[-1]
    return _is_time_proximate(msg, last_msg)


def _is_time_proximate(msg: dict[str, Any], last_msg: dict[str, Any]) -> bool:
    """Check if two messages are from the same author within time threshold.

    Args:
        msg: The candidate message.
        last_msg: The last message in the current group.

    Returns:
        ``True`` if same author and within the time proximity threshold.
    """
    msg_author = _safe_author_name(msg)
    last_author = _safe_author_name(last_msg)
    if msg_author != last_author:
        return False

    msg_ts = _parse_msg_timestamp(msg)
    last_ts = _parse_msg_timestamp(last_msg)
    if msg_ts is None or last_ts is None:
        return False

    delta = abs(msg_ts - last_ts)
    return delta <= timedelta(minutes=TIME_PROXIMITY_MINUTES)


# ---- DiscordIngestor ----


class DiscordIngestor(Ingestor):
    """Ingestor for Discord data export packages.

    Processes per-channel ``messages.json`` and ``channel.json`` files
    from Discord data exports. Groups messages by reply chains and
    time proximity, then converts each group into a Creek fragment.
    """

    def discover(self, source_path: Path) -> list[RawDocument]:
        """Find all ``messages.json`` files within channel directories.

        Expects the Discord export structure::

            source_path/messages/{channel_id}/messages.json
            source_path/messages/{channel_id}/channel.json

        Args:
            source_path: Root directory of the Discord data export.

        Returns:
            A list of ``RawDocument`` objects, one per channel.
        """
        docs: list[RawDocument] = []
        messages_dir = source_path / "messages"
        if not messages_dir.is_dir():
            return docs

        for channel_dir in sorted(messages_dir.iterdir()):
            if not channel_dir.is_dir():
                continue
            messages_file = channel_dir / "messages.json"
            if not messages_file.is_file():
                continue

            raw_bytes = messages_file.read_bytes()
            metadata = self._load_channel_metadata(channel_dir)
            metadata["channel_dir"] = str(channel_dir)

            docs.append(
                RawDocument(
                    path=messages_file,
                    content=raw_bytes,
                    metadata=metadata,
                    detected_encoding="utf-8",
                )
            )

        return docs

    def _load_channel_metadata(self, channel_dir: Path) -> dict[str, Any]:
        """Load channel metadata from ``channel.json`` if it exists.

        Args:
            channel_dir: The directory containing channel files.

        Returns:
            A dict of channel metadata, or defaults if file is missing.
        """
        channel_file = channel_dir / "channel.json"
        if channel_file.is_file():
            try:
                data = json.loads(channel_file.read_bytes())
                return {
                    "channel_id": str(data.get("id", channel_dir.name)),
                    "channel_name": str(data.get("name", channel_dir.name)),
                    "channel_type": str(data.get("type", "text")),
                }
            except (json.JSONDecodeError, OSError):
                logger.warning("Failed to parse channel.json in %s", channel_dir)

        return {
            "channel_id": channel_dir.name,
            "channel_name": channel_dir.name,
            "channel_type": "unknown",
        }

    def parse(self, raw: RawDocument) -> list[ParsedFragment]:
        """Extract message groups as fragments from a channel's messages.

        Groups messages by reply chains and time proximity, then creates
        one ``ParsedFragment`` per group.

        Args:
            raw: The raw document containing messages JSON.

        Returns:
            A list of ``ParsedFragment`` objects, one per message group.
        """
        text = raw.content.decode(raw.detected_encoding, errors="replace")
        messages = self._parse_messages_json(text, raw.path)
        if not messages:
            return []

        msg_index = _build_message_index(messages)
        groups = _group_messages(messages)
        channel_name = raw.metadata.get("channel_name", "unknown")
        channel_id = raw.metadata.get("channel_id", "unknown")

        fragments: list[ParsedFragment] = []
        for group in groups:
            fragment = self._group_to_fragment(
                group=group,
                msg_index=msg_index,
                channel_name=channel_name,
                channel_id=channel_id,
                source_path=str(raw.path),
            )
            if fragment is not None:
                fragments.append(fragment)

        return fragments

    def _parse_messages_json(self, text: str, path: Path) -> list[dict[str, Any]]:
        """Parse the messages JSON text, handling both array and object formats.

        Supports two formats:

        1. A bare JSON array of messages: ``[{...}, ...]``
        2. An object with a ``"messages"`` key: ``{"messages": [{...}, ...]}``

        Args:
            text: The JSON text content.
            path: The file path (for error messages).

        Returns:
            A list of message dicts, or empty list on parse failure.
        """
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            logger.warning("Failed to parse messages JSON at %s", path)
            return []

        if isinstance(data, list):
            return data
        if isinstance(data, dict) and "messages" in data:
            msgs = data["messages"]
            if isinstance(msgs, list):
                return msgs
        return []

    def _group_to_fragment(
        self,
        group: list[dict[str, Any]],
        msg_index: dict[str, dict[str, Any]],
        channel_name: str,
        channel_id: str,
        source_path: str,
    ) -> ParsedFragment | None:
        """Convert a message group into a ParsedFragment.

        Args:
            group: The list of message dicts in this group.
            msg_index: Lookup index for all messages (for reply context).
            channel_name: The channel display name.
            channel_id: The channel ID string.
            source_path: Path to the source file.

        Returns:
            A ``ParsedFragment``, or ``None`` if the group has no content.
        """
        content_parts: list[str] = []
        authors: set[str] = set()

        for msg in group:
            part = self._format_message(msg, msg_index)
            if part:
                content_parts.append(part)
            authors.add(_safe_author_name(msg))

        if not content_parts:
            return None

        content = "\n\n".join(content_parts)
        first_ts = _safe_timestamp(group[0])
        timestamp = self._resolve_timestamp(first_ts)

        return ParsedFragment(
            content=content,
            metadata={
                "channel_name": channel_name,
                "channel_id": channel_id,
                "authors": sorted(authors),
                "message_count": len(group),
                "message_ids": [str(m.get("id", "")) for m in group],
            },
            source_path=source_path,
            timestamp=timestamp,
        )

    def _format_message(
        self,
        msg: dict[str, Any],
        msg_index: dict[str, dict[str, Any]],
    ) -> str:
        """Format a single Discord message as Markdown text.

        Includes reply context (quoted parent) if the message is a reply.

        Args:
            msg: The message dict to format.
            msg_index: Lookup index for resolving reply parents.

        Returns:
            A formatted Markdown string for this message.
        """
        parts: list[str] = []

        # Add reply context if this is a reply
        ref_id = _get_reference_id(msg)
        if ref_id is not None and ref_id in msg_index:
            parts.append(_format_reply_context(msg_index[ref_id]))
            parts.append("")  # blank line after quote

        author = _safe_author_name(msg)
        content = _format_discord_content(msg.get("content", ""))

        parts.append(f"**{author}**: {content}")

        # Handle embeds
        embeds = msg.get("embeds")
        if isinstance(embeds, list):
            for embed in embeds:
                embed_text = self._format_embed(embed)
                if embed_text:
                    parts.append(embed_text)

        # Handle reactions
        reactions = msg.get("reactions")
        if isinstance(reactions, list) and reactions:
            reaction_text = self._format_reactions(reactions)
            if reaction_text:
                parts.append(reaction_text)

        return "\n".join(parts)

    def _format_embed(self, embed: Any) -> str:
        """Format a Discord embed as Markdown.

        Args:
            embed: The embed object (expected to be a dict).

        Returns:
            A Markdown-formatted string, or empty string if not a dict.
        """
        if not isinstance(embed, dict):
            return ""

        parts: list[str] = []
        title = embed.get("title")
        if title:
            parts.append(f"  *[Embed: {title}]*")
        description = embed.get("description")
        if description:
            parts.append(f"  > {description}")
        url = embed.get("url")
        if url:
            parts.append(f"  Link: {url}")

        return "\n".join(parts)

    def _format_reactions(self, reactions: list[Any]) -> str:
        """Format message reactions as a compact text line.

        Args:
            reactions: List of reaction objects.

        Returns:
            A formatted reactions line, or empty string if none valid.
        """
        parts: list[str] = []
        for reaction in reactions:
            if not isinstance(reaction, dict):
                continue
            emoji = reaction.get("emoji", {})
            name = emoji.get("name", "?") if isinstance(emoji, dict) else str(emoji)
            count = reaction.get("count", 1)
            parts.append(f"{name} x{count}")

        if not parts:
            return ""
        return f"Reactions: {', '.join(parts)}"

    def _resolve_timestamp(self, ts_str: str) -> datetime:
        """Resolve a timestamp string to a normalized datetime.

        Falls back to epoch if parsing fails.

        Args:
            ts_str: The ISO 8601 timestamp string.

        Returns:
            A timezone-aware datetime in the configured timezone.
        """
        if not ts_str:
            return normalize_timestamp("1970-01-01T00:00:00Z", None)
        try:
            return normalize_timestamp(ts_str, None)
        except ValueError:
            return normalize_timestamp("1970-01-01T00:00:00Z", None)

    def convert_to_markdown(self, fragment: ParsedFragment) -> str:
        """Convert a parsed Discord fragment to clean Markdown.

        Adds the channel name as a header and preserves the formatted
        message content.

        Args:
            fragment: The parsed fragment to convert.

        Returns:
            A Markdown-formatted string with channel header.
        """
        channel = fragment.metadata.get("channel_name", "unknown")
        header = f"# #{channel}\n\n"
        return header + fragment.content

    def generate_frontmatter(self, fragment: ParsedFragment) -> dict[str, Any]:
        """Generate YAML frontmatter metadata for a Discord fragment.

        Produces frontmatter with source platform, channel, timestamps,
        and participant information.

        Args:
            fragment: The parsed fragment.

        Returns:
            A dict of frontmatter key-value pairs.
        """
        return {
            "source": {
                "platform": "discord",
                "channel": fragment.metadata.get("channel_name", "unknown"),
                "channel_id": fragment.metadata.get("channel_id", "unknown"),
            },
            "created": fragment.timestamp.isoformat(),
            "authors": fragment.metadata.get("authors", []),
            "message_count": fragment.metadata.get("message_count", 0),
        }
