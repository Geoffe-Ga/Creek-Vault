"""Claude conversation JSON ingestor for the Creek ingest pipeline.

This module provides ``ClaudeIngestor``, a concrete ``Ingestor`` subclass
that reads Claude (claude.ai) conversation exports in JSON format and
produces ``ParsedFragment`` objects for downstream processing.

Supported export formats:

- **Multi-conversation**: ``{"conversations": [{"uuid": ..., "messages": ...}, ...]}``
- **Single-conversation**: ``{"conversation_id": ..., "messages": [...]}``

Each human+assistant turn pair becomes one fragment. System prompts are
skipped. Timestamps are normalized to America/Los_Angeles.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from creek.ingest.base import (
    Ingestor,
    ParsedFragment,
    RawDocument,
    normalize_timestamp,
)

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

# ---- Content Extraction ----


def _extract_text(content: str | list[dict[str, Any]]) -> str:
    """Extract plain text from a message content field.

    Claude exports may represent content as a plain string or as a
    list of typed parts (e.g., ``[{"type": "text", "text": "..."}]``).

    Args:
        content: The message content, either a string or a list of parts.

    Returns:
        The extracted text as a single string.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                parts.append(str(part.get("text", "")))
        return "\n".join(parts)
    return str(content)


# ---- Conversation Normalization ----


def _normalize_conversations(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Normalize raw JSON data into a list of conversation dicts.

    Handles both multi-conversation (``{"conversations": [...]}``}) and
    single-conversation (``{"conversation_id": ..., "messages": [...]}``})
    export formats.

    Args:
        data: The parsed JSON data from a Claude export file.

    Returns:
        A list of normalized conversation dicts with ``uuid``, ``name``,
        ``created_at``, and ``messages`` keys.
    """
    if "conversations" in data:
        return [_normalize_single(c) for c in data["conversations"]]
    if "messages" in data:
        return [_normalize_single(data)]
    return []


def _normalize_single(conv: dict[str, Any]) -> dict[str, Any]:
    """Normalize a single conversation dict to a canonical form.

    Maps variant field names (``conversation_id`` vs ``uuid``, ``title``
    vs ``name``, ``create_time`` vs ``created_at``, ``timestamp`` vs
    ``created_at`` on messages) to a consistent schema.

    Args:
        conv: A raw conversation dict from the export.

    Returns:
        A normalized conversation dict.
    """
    uuid = conv.get("uuid") or conv.get("conversation_id") or "unknown"
    name = conv.get("name") or conv.get("title") or "Untitled"
    created_at = conv.get("created_at") or conv.get("create_time") or ""
    model = conv.get("model")
    messages = _normalize_messages(conv.get("messages", []))

    result: dict[str, Any] = {
        "uuid": uuid,
        "name": name,
        "created_at": created_at,
        "messages": messages,
    }
    if model:
        result["model"] = model
    return result


def _normalize_messages(
    messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Normalize message dicts to use consistent field names.

    Maps ``timestamp`` to ``created_at`` if the latter is absent.

    Args:
        messages: List of raw message dicts.

    Returns:
        List of normalized message dicts.
    """
    normalized: list[dict[str, Any]] = []
    for msg in messages:
        m: dict[str, Any] = dict(msg)
        if "created_at" not in m and "timestamp" in m:
            m["created_at"] = m["timestamp"]
        normalized.append(m)
    return normalized


# ---- Turn Pairing ----


def _pair_turns(
    messages: list[dict[str, Any]],
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """Pair human turns with their corresponding assistant responses.

    Skips system prompts. Consecutive human messages are merged (only the
    last is kept). A trailing human message without an assistant response
    is discarded.

    Args:
        messages: List of normalized message dicts.

    Returns:
        List of (human_message, assistant_message) tuples.
    """
    pairs: list[tuple[dict[str, Any], dict[str, Any]]] = []
    pending_human: dict[str, Any] | None = None

    for msg in messages:
        role = msg.get("role", "")
        if role == "system":
            continue
        if role == "human":
            pending_human = msg
        elif role == "assistant" and pending_human is not None:
            pairs.append((pending_human, msg))
            pending_human = None

    return pairs


# ---- Timestamp Resolution ----


def _resolve_timestamp(msg: dict[str, Any], fallback: str) -> str:
    """Resolve the timestamp for a message, falling back to a default.

    Args:
        msg: The message dict that may contain a ``created_at`` field.
        fallback: Fallback timestamp string to use if the message has none.

    Returns:
        A timestamp string.
    """
    ts = msg.get("created_at")
    if ts:
        return str(ts)
    return fallback if fallback else "2000-01-01T00:00:00Z"


# ---- Claude Ingestor ----


class ClaudeIngestor(Ingestor):
    """Ingestor for Claude (claude.ai) conversation JSON exports.

    Reads Claude export files (JSON) and produces one ``ParsedFragment``
    per human+assistant turn pair. System prompts are skipped. Timestamps
    are normalized to America/Los_Angeles.

    The ingestor supports both the multi-conversation wrapper format
    (``{"conversations": [...]}``}) and the single-conversation format
    (``{"conversation_id": ..., "messages": [...]}``}).
    """

    def discover(self, source_path: Path) -> list[RawDocument]:
        """Find all Claude export JSON files in the given directory.

        Scans ``source_path`` for ``.json`` files, reads each, and checks
        whether it matches a known Claude export format (has a
        ``conversations`` key or both ``messages`` and either
        ``conversation_id`` or ``uuid``).

        Args:
            source_path: Directory to scan for Claude export files.

        Returns:
            A list of ``RawDocument`` objects for each valid export file.
        """
        docs: list[RawDocument] = []
        for json_path in sorted(source_path.glob("*.json")):
            raw = self._try_read_export(json_path)
            if raw is not None:
                docs.append(raw)
        return docs

    def parse(self, raw: RawDocument) -> list[ParsedFragment]:
        """Extract conversation turn pairs from a Claude export document.

        Each human+assistant turn pair becomes one ``ParsedFragment``.
        System messages are skipped. Trailing human turns without
        assistant responses are discarded.

        Args:
            raw: A ``RawDocument`` containing Claude export JSON bytes.

        Returns:
            A list of ``ParsedFragment`` objects, one per turn pair.
        """
        data = json.loads(raw.content.decode(raw.detected_encoding))
        conversations = _normalize_conversations(data)
        fragments: list[ParsedFragment] = []

        for conv in conversations:
            conv_fragments = self._parse_conversation(conv, str(raw.path))
            fragments.extend(conv_fragments)

        return fragments

    def convert_to_markdown(self, fragment: ParsedFragment) -> str:
        """Convert a parsed fragment to Markdown with blockquote formatting.

        The human turn is rendered as a blockquote (each line prefixed
        with ``>``), followed by a blank line, then the assistant response
        as plain text.

        Args:
            fragment: A parsed fragment containing human and assistant text.

        Returns:
            A Markdown-formatted string.
        """
        human_text = fragment.metadata.get("human_text", "")
        assistant_text = fragment.metadata.get("assistant_text", "")

        human_lines = human_text.split("\n")
        blockquote = "\n".join(f"> {line}" for line in human_lines)

        return f"{blockquote}\n\n{assistant_text}"

    def generate_frontmatter(self, fragment: ParsedFragment) -> dict[str, Any]:
        """Generate YAML frontmatter metadata for a Claude conversation fragment.

        Produces a dict with:

        - ``title``: Conversation name with turn index
        - ``created``: ISO timestamp in America/Los_Angeles
        - ``source.platform``: ``"claude"``
        - ``source.conversation_id``: The conversation UUID
        - ``source.model``: The model used (if available)

        Args:
            fragment: A parsed fragment with conversation metadata.

        Returns:
            A dict of frontmatter key-value pairs.
        """
        meta = fragment.metadata
        conv_name = meta.get("conversation_name", "Untitled")
        turn_idx = meta.get("turn_index", 0)
        conv_id = meta.get("conversation_id", "unknown")
        model = meta.get("model")

        source: dict[str, Any] = {
            "platform": "claude",
            "conversation_id": conv_id,
        }
        if model is not None:
            source["model"] = model

        return {
            "title": f"{conv_name} (turn {turn_idx})",
            "created": fragment.timestamp.isoformat(),
            "source": source,
        }

    # ---- Private Helpers ----

    def _try_read_export(self, json_path: Path) -> RawDocument | None:
        """Attempt to read and validate a JSON file as a Claude export.

        Args:
            json_path: Path to a candidate JSON file.

        Returns:
            A ``RawDocument`` if the file is a valid Claude export,
            or ``None`` if it is not.
        """
        try:
            content = json_path.read_bytes()
            data = json.loads(content)
        except (json.JSONDecodeError, OSError):
            logger.debug("Skipping non-JSON or unreadable file: %s", json_path)
            return None

        if not isinstance(data, dict):
            return None

        if not self._is_claude_export(data):
            return None

        return RawDocument(
            path=json_path,
            content=content,
            metadata={},
            detected_encoding="utf-8",
        )

    @staticmethod
    def _is_claude_export(data: dict[str, Any]) -> bool:
        """Check whether a parsed JSON dict looks like a Claude export.

        Args:
            data: Parsed JSON data.

        Returns:
            True if the data matches a known Claude export format.
        """
        if "conversations" in data:
            return True
        return "messages" in data and ("conversation_id" in data or "uuid" in data)

    def _parse_conversation(
        self,
        conv: dict[str, Any],
        source_path: str,
    ) -> list[ParsedFragment]:
        """Parse a single normalized conversation into fragments.

        Args:
            conv: A normalized conversation dict.
            source_path: The original file path string.

        Returns:
            A list of ParsedFragment objects.
        """
        messages = conv.get("messages", [])
        pairs = _pair_turns(messages)
        fallback_ts = conv.get("created_at", "2000-01-01T00:00:00Z")
        conv_id = conv.get("uuid", "unknown")
        conv_name = conv.get("name", "Untitled")
        model = conv.get("model")

        fragments: list[ParsedFragment] = []
        for idx, (human_msg, assistant_msg) in enumerate(pairs):
            fragment = self._build_fragment(
                human_msg=human_msg,
                assistant_msg=assistant_msg,
                turn_index=idx,
                conv_id=conv_id,
                conv_name=conv_name,
                model=model,
                fallback_ts=fallback_ts,
                source_path=source_path,
            )
            fragments.append(fragment)

        return fragments

    def _build_fragment(
        self,
        *,
        human_msg: dict[str, Any],
        assistant_msg: dict[str, Any],
        turn_index: int,
        conv_id: str,
        conv_name: str,
        model: str | None,
        fallback_ts: str,
        source_path: str,
    ) -> ParsedFragment:
        """Build a single ParsedFragment from a human+assistant turn pair.

        Args:
            human_msg: The human message dict.
            assistant_msg: The assistant message dict.
            turn_index: Zero-based index of this turn pair.
            conv_id: Conversation UUID.
            conv_name: Conversation name/title.
            model: Model used for the conversation.
            fallback_ts: Fallback timestamp if messages lack timestamps.
            source_path: Original file path.

        Returns:
            A ParsedFragment for this turn pair.
        """
        human_text = _extract_text(human_msg.get("content", ""))
        assistant_text = _extract_text(assistant_msg.get("content", ""))

        ts_string = _resolve_timestamp(human_msg, fallback_ts)
        timestamp = normalize_timestamp(ts_string, source_tz=None)

        content = f"{human_text}\n\n{assistant_text}"

        metadata: dict[str, Any] = {
            "conversation_id": conv_id,
            "conversation_name": conv_name,
            "turn_index": turn_index,
            "human_text": human_text,
            "assistant_text": assistant_text,
        }
        if model is not None:
            metadata["model"] = model

        return ParsedFragment(
            content=content,
            metadata=metadata,
            source_path=source_path,
            timestamp=timestamp,
        )
