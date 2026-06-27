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
import shutil
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

from creek.clean.filters.discord import DiscordFilter, DiscordFilterConfig
from creek.ingest.base import (
    Ingestor,
    ParsedFragment,
    RawDocument,
    normalize_timestamp,
    parse_authored_at,
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


_SELF_AUTHOR_NAME = "self"
"""Author name for messages from the current data package (the owner's own)."""


def _normalize_timestamp(timestamp: str) -> str:
    """Coerce a data-package timestamp (``"YYYY-MM-DD HH:MM:SS"``) to ISO 8601.

    The current Discord data package uses a space-separated, timezone-naive
    timestamp; :func:`datetime.fromisoformat` wants a ``T`` separator and the
    timestamps are UTC, so a ``+00:00`` offset is appended when none is present.
    """
    iso = timestamp.strip()
    if not iso:
        return iso
    iso = iso.replace(" ", "T", 1)
    if "+" not in iso[10:] and "Z" not in iso[10:]:
        iso = f"{iso}+00:00"
    return iso


def _normalize_message(msg: dict[str, Any]) -> dict[str, Any]:
    """Normalize a Discord message to the canonical lowercase schema (#593).

    The current Discord data package uses capitalized flat keys
    (``ID`` / ``Timestamp`` / ``Contents``) and carries no author field — the
    package contains only the requester's own messages. Map those to the
    ``id`` / ``timestamp`` / ``content`` / ``author`` schema the rest of the
    ingestor reads, normalising the timestamp to ISO 8601 and attributing the
    message to the export owner. Messages already in the lowercase schema
    (older exports / DiscordChatExporter) are returned unchanged.

    Args:
        msg: A raw Discord message dict.

    Returns:
        The message in the canonical lowercase schema.
    """
    if "content" in msg or "id" in msg:
        return msg
    normalized = msg.copy()
    if "Contents" in msg:
        normalized["content"] = msg.get("Contents", "")
    if "ID" in msg:
        normalized["id"] = str(msg.get("ID", ""))
    if "Timestamp" in msg:
        normalized["timestamp"] = _normalize_timestamp(str(msg.get("Timestamp", "")))
    normalized.setdefault("author", {"name": _SELF_AUTHOR_NAME})
    return normalized


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

    Optionally applies a :class:`DiscordFilter` to skip low-value
    messages before fragment creation.

    Attributes:
        discord_filter: The pre-ingestion filter instance.
    """

    def __init__(
        self,
        discord_filter_config: DiscordFilterConfig | None = None,
    ) -> None:
        """Initialise the ingestor with optional filter configuration.

        Args:
            discord_filter_config: Configuration for pre-ingestion
                filtering.  Uses defaults (all filters enabled) if
                ``None``.
        """
        self.discord_filter = DiscordFilter(
            config=discord_filter_config,
        )

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
            except (json.JSONDecodeError, OSError):
                logger.warning("Failed to parse channel.json in %s", channel_dir)
            else:
                return {
                    "channel_id": str(data.get("id", channel_dir.name)),
                    "channel_name": str(data.get("name", channel_dir.name)),
                    "channel_type": str(data.get("type", "text")),
                }

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

        # Apply pre-ingestion filter to remove noise before grouping
        messages, _stats = self.discord_filter.filter_messages(messages)
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
            return [_normalize_message(m) for m in data if isinstance(m, dict)]
        if isinstance(data, dict) and "messages" in data:
            msgs = data["messages"]
            if isinstance(msgs, list):
                return [_normalize_message(m) for m in msgs if isinstance(m, dict)]
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
        # FEAT-031: ``authored_at`` is the source-side message timestamp,
        # preserved with its native tz. ``timestamp`` (LA-anchored) is
        # what the ID hash and other LA-tied surfaces use; the two
        # carry the same instant but in different timezones.
        authored_at = self._resolve_authored_at(first_ts)

        return ParsedFragment(
            content=content,
            metadata={
                "channel_name": channel_name,
                "channel_id": channel_id,
                "authors": sorted(authors),
                "message_count": len(group),
                "message_ids": [str(m.get("id", "")) for m in group],
                "authored_at": authored_at,
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
            # blank line after quote
            parts.extend((_format_reply_context(msg_index[ref_id]), ""))

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

    def _resolve_authored_at(self, ts_str: str) -> datetime | None:
        """Resolve a Discord message ``timestamp`` into ``authored_at`` (FEAT-031).

        Discord exports stamp every message with an ISO-8601 timestamp
        in its ``timestamp`` field — the canonical source-side answer
        to "when was this said?". Returns ``None`` only when the field
        is missing or unparseable; never guesses a fallback (the
        epoch sentinel used by :meth:`_resolve_timestamp` for the
        ID-hash path is deliberately not reused here).
        """
        if not ts_str:
            return None
        try:
            return parse_authored_at(ts_str)
        except ValueError:
            logger.warning("Discord message has unparseable timestamp %r", ts_str)
            return None

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
        and participant information. FEAT-031: the message's source-side
        ``timestamp`` (preserved with its native offset) lands on
        ``authored_at``.

        Args:
            fragment: The parsed fragment.

        Returns:
            A dict of frontmatter key-value pairs.
        """
        frontmatter_dict: dict[str, Any] = {
            "source": {
                "platform": "discord",
                "channel": fragment.metadata.get("channel_name", "unknown"),
                "channel_id": fragment.metadata.get("channel_id", "unknown"),
            },
            "created": fragment.timestamp.isoformat(),
            "authors": fragment.metadata.get("authors", []),
            "message_count": fragment.metadata.get("message_count", 0),
        }
        authored_at: datetime | None = fragment.metadata.get("authored_at")
        if authored_at is not None:
            frontmatter_dict["authored_at"] = authored_at.isoformat()
        return frontmatter_dict


# ---- Bot-capture consumption (#687) ----
#
# The CrawDad bot-capture path logs each message to
# ``<capture_dir>/<channel>/<date>.jsonl`` — one JSON object per line in the
# lowercase message schema this ingestor already reads (``id`` / ``timestamp`` /
# ``content`` / ``author`` / optional ``reference``). To consume it without a
# parser rewrite, the helpers below reshape the capture dir into the
# Data-Package layout (``messages/<channel>/messages.json`` + ``channel.json``)
# that :meth:`DiscordIngestor.discover` expects.


def _capture_sort_key(message: dict[str, Any]) -> str:
    """Chronological sort key — the ISO-8601 ``timestamp`` (UTC, so lexical)."""
    return str(message.get("timestamp", ""))


def _read_capture_messages(channel_dir: Path) -> list[dict[str, Any]]:
    """Read every JSONL line under one capture channel dir, deduped and sorted.

    Discord message ids are stable, so a line repeated across the live stream
    and a backfill collapses to one message (keyed by id). Returns the messages
    sorted chronologically by ``timestamp`` so reply/time grouping stays correct.

    Args:
        channel_dir: A ``<capture_dir>/<channel>/`` directory of ``*.jsonl``.

    Returns:
        The deduped, chronologically sorted message dicts.
    """
    by_id: dict[str, dict[str, Any]] = {}
    for jsonl in sorted(channel_dir.glob("*.jsonl")):
        for line in jsonl.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            try:
                obj = json.loads(stripped)
            except json.JSONDecodeError:
                logger.warning("Skipping malformed capture line in %s", jsonl)
                continue
            if isinstance(obj, dict) and obj.get("id"):
                by_id[str(obj["id"])] = obj
    return sorted(by_id.values(), key=_capture_sort_key)


def stage_capture_as_data_package(capture_dir: Path, staging_dir: Path) -> None:
    """Reshape a ``discord-capture/`` dir into the Data-Package layout (#687).

    Writes ``staging_dir/messages/<channel>/messages.json`` (deduped by stable
    message id, chronologically sorted) plus a ``channel.json`` so the existing
    :class:`DiscordIngestor` reads the captured messages with no parser rewrite.

    Args:
        capture_dir: The ``<vault>/discord-capture`` root the bot appends to.
        staging_dir: A scratch dir to materialise the Data-Package layout into.
    """
    if not capture_dir.is_dir():
        return
    for channel_dir in sorted(capture_dir.iterdir()):
        if not channel_dir.is_dir():
            continue
        messages = _read_capture_messages(channel_dir)
        if not messages:
            continue
        channel = channel_dir.name
        out_dir = staging_dir / "messages" / channel
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "messages.json").write_text(json.dumps(messages), encoding="utf-8")
        (out_dir / "channel.json").write_text(
            json.dumps({"id": channel, "name": channel}), encoding="utf-8"
        )


def ingest_capture_dir(capture_dir: Path, vault: Path) -> int:
    """Stage + ingest a bot-capture dir into *vault*; return fragments written.

    Idempotent: the messages stage into a **stable** vault-relative path, so the
    ``frag-<sha>`` id (which hashes the staged source path) is identical across
    runs. Combined with stable Discord message ids, re-running over an
    overlapping capture writes no duplicate fragments.

    Args:
        capture_dir: The ``<vault>/discord-capture`` root the bot appends to.
        vault: The Creek vault to write fragments into.

    Returns:
        The number of fragments written this run.
    """
    from creek.ingest.base import assemble_ingested_fragment
    from creek.vault.writer import VaultWriter

    # Stable, deterministic staging path (not a random tempdir) so the
    # source-path-derived fragment id stays constant across runs. A kill between
    # the rmtree and ingest leaves an empty/partial staging dir and writes zero
    # fragments — self-healing: the next run re-stages from the intact capture
    # dir, so a one-off "0 fragments" in the logs is not data loss.
    staging = vault / "00-Creek-Meta" / "State" / "discord" / "capture-staging"
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True, exist_ok=True)
    stage_capture_as_data_package(capture_dir, staging)
    result = DiscordIngestor().ingest(staging)
    writer = VaultWriter(vault_path=vault)
    count = 0
    for parsed in result.fragments:
        assembled = assemble_ingested_fragment(parsed)
        writer.write_fragment(assembled.fragment, body=assembled.body)
        count += 1
    return count
