"""ChatGPT conversation JSON ingestor for the Creek ingest pipeline.

Parses ChatGPT's tree-structured JSON export format into Creek fragments.
ChatGPT exports contain a list of conversations, each with a ``mapping``
dict of message nodes connected by ``parent``/``children`` references.

The ingestor:

1. **Discovers** ``.json`` files in a source directory.
2. **Parses** the tree-structured ``mapping`` to extract ordered
   user+assistant message pairs, following the longest branch
   when conversations branch.
3. **Converts** each pair to blockquote-formatted Markdown.
4. **Generates** YAML frontmatter with ``source.platform: "chatgpt"``.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from creek.ingest.base import (
    LA_TZ,
    Ingestor,
    ParsedFragment,
    RawDocument,
    normalize_encoding,
)

if TYPE_CHECKING:
    from pathlib import Path

    from creek.clean.filters.chatbot import ChatbotFilter

logger = logging.getLogger(__name__)


class ChatGPTIngestor(Ingestor):
    """Ingestor for ChatGPT JSON conversation exports.

    Handles the tree-structured ``mapping`` format used by ChatGPT's
    data export feature, where messages are connected via
    ``parent``/``children`` references.

    An optional :class:`ChatbotFilter` can be provided to filter noise
    (system prompts, tool outputs, regenerations) before turn pairing.
    """

    def __init__(
        self,
        *,
        chatbot_filter: ChatbotFilter | None = None,
    ) -> None:
        """Initialise the ChatGPT ingestor with an optional chatbot filter.

        Args:
            chatbot_filter: Optional pre-ingestion filter for noise removal.
                If ``None``, no filtering is applied.
        """
        self._chatbot_filter = chatbot_filter

    def discover(self, source_path: Path) -> list[RawDocument]:
        """Find ChatGPT JSON export files in the given directory.

        Scans *source_path* for ``.json`` files and returns a
        ``RawDocument`` for each one whose content is a JSON list of
        dicts (the ChatGPT export format).  Files that contain other
        JSON structures (e.g. a top-level dict) are silently skipped.

        Args:
            source_path: Directory to search for JSON export files.

        Returns:
            A list of ``RawDocument`` objects for each discovered file.
        """
        if not source_path.is_dir():
            return []

        docs: list[RawDocument] = []
        for json_file in sorted(source_path.glob("*.json")):
            raw_bytes = json_file.read_bytes()
            _text, encoding = normalize_encoding(raw_bytes)
            if not _is_chatgpt_export(raw_bytes, encoding):
                logger.debug("Skipping non-ChatGPT JSON file: %s", json_file)
                continue
            docs.append(
                RawDocument(
                    path=json_file,
                    content=raw_bytes,
                    metadata={"source_type": "chatgpt"},
                    detected_encoding=encoding,
                )
            )
        return docs

    def parse(self, raw: RawDocument) -> list[ParsedFragment]:
        """Extract conversation turns from a ChatGPT JSON export.

        Decodes the JSON content, iterates over conversations, and
        extracts user+assistant message pairs by traversing the
        tree-structured ``mapping``. For branching conversations,
        the branch with the most messages is followed.

        Args:
            raw: A raw document containing ChatGPT JSON data.

        Returns:
            A list of ``ParsedFragment`` objects, one per user+assistant pair.
        """
        text = raw.content.decode(raw.detected_encoding, errors="replace")
        conversations: list[dict[str, Any]] = json.loads(text)

        fragments: list[ParsedFragment] = []
        for conv in conversations:
            conv_fragments = self._parse_conversation(conv, str(raw.path))
            fragments.extend(conv_fragments)
        return fragments

    def convert_to_markdown(self, fragment: ParsedFragment) -> str:
        """Convert a parsed fragment to blockquote-formatted Markdown.

        Wraps the fragment content in a blockquote and prepends the
        conversation title as a heading.

        Args:
            fragment: The parsed fragment to convert.

        Returns:
            A Markdown-formatted string with title and blockquoted content.
        """
        title = fragment.metadata.get("title", "Untitled Conversation")
        lines = fragment.content.split("\n")
        blockquoted = "\n".join(f"> {line}" if line.strip() else ">" for line in lines)
        return f"# {title}\n\n{blockquoted}\n"

    def generate_frontmatter(self, fragment: ParsedFragment) -> dict[str, Any]:
        """Generate YAML frontmatter metadata for a ChatGPT fragment.

        Produces frontmatter with ``source.platform`` set to ``"chatgpt"``
        and includes the conversation title, creation timestamp, and
        original file path. FEAT-031: the per-message ``create_time``
        (falling back to the conversation's ``create_time``) lands on
        ``authored_at`` in UTC.

        Args:
            fragment: The parsed fragment to generate frontmatter for.

        Returns:
            A dict of frontmatter key-value pairs.
        """
        source: dict[str, Any] = {
            "platform": "chatgpt",
            "original_file": fragment.source_path,
        }
        conv_id = fragment.metadata.get("conversation_id")
        if conv_id is not None:
            source["conversation_id"] = conv_id
        frontmatter_dict: dict[str, Any] = {
            "title": fragment.metadata.get("title", "Untitled Conversation"),
            "created": fragment.timestamp.isoformat(),
            "source": source,
        }
        authored_at: datetime | None = fragment.metadata.get("authored_at")
        if authored_at is not None:
            frontmatter_dict["authored_at"] = authored_at.isoformat()
        return frontmatter_dict

    # ---- Private helpers ----

    def _parse_conversation(
        self,
        conv: dict[str, Any],
        source_path: str,
    ) -> list[ParsedFragment]:
        """Parse a single ChatGPT conversation into fragments.

        Traverses the tree-structured mapping to extract an ordered
        list of messages, then pairs user+assistant messages into
        fragments.

        Args:
            conv: A single conversation dict from the ChatGPT export.
            source_path: The file path of the source document.

        Returns:
            A list of ``ParsedFragment`` objects for this conversation.
        """
        mapping = conv.get("mapping")
        if not mapping:
            return []

        title = conv.get("title", "Untitled Conversation")
        create_time = conv.get("create_time", 0.0)
        timestamp = _epoch_to_la_datetime(create_time)
        conversation_id: str | None = conv.get("id")

        ordered_messages = _linearize_tree(mapping)

        # Apply chatbot filter if configured
        if self._chatbot_filter is not None:
            normalized = _normalize_chatgpt_messages(ordered_messages)
            filter_result = self._chatbot_filter.filter_conversation(
                normalized, platform="chatgpt"
            )
            ordered_messages = _denormalize_chatgpt_messages(
                filter_result.messages, ordered_messages
            )

        return _pair_messages_to_fragments(
            ordered_messages,
            title,
            timestamp,
            source_path,
            conversation_id,
            conversation_create_time=create_time,
        )


def _normalize_chatgpt_messages(
    messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Normalize ChatGPT message dicts for the chatbot filter.

    Converts ChatGPT's ``author.role`` / ``content.parts`` structure
    into the flat ``role`` / ``content`` format expected by
    :class:`ChatbotFilter`.

    Args:
        messages: List of raw ChatGPT message dicts.

    Returns:
        List of normalized message dicts with ``role`` and ``content`` keys.
    """
    normalized: list[dict[str, Any]] = []
    for msg in messages:
        role = _get_message_role(msg)
        text = _extract_message_text(msg)
        normalized.append({"role": role, "content": text, "_original": msg})
    return normalized


def _denormalize_chatgpt_messages(
    filtered: list[dict[str, Any]],
    originals: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Map filtered normalized messages back to original ChatGPT format.

    Uses the ``_original`` reference stored during normalization to
    return the original message dicts that passed filtering.

    Args:
        filtered: Filtered normalized messages from the chatbot filter.
        originals: The original unfiltered ChatGPT messages.

    Returns:
        List of original ChatGPT message dicts that passed filtering.
    """
    result: list[dict[str, Any]] = []
    for msg in filtered:
        original = msg.get("_original")
        if original is not None:
            result.append(original)
    return result


def _is_chatgpt_export(raw_bytes: bytes, encoding: str) -> bool:
    """Check whether *raw_bytes* looks like a ChatGPT conversation export.

    A valid ChatGPT export is a JSON array whose elements are dicts
    (each representing a conversation).  Any other top-level JSON
    structure (e.g. a dict from a Claude export) is rejected.

    Args:
        raw_bytes: The raw file content.
        encoding: The detected character encoding.

    Returns:
        ``True`` if the content is a JSON list of dicts, ``False`` otherwise.
    """
    try:
        data = json.loads(raw_bytes.decode(encoding, errors="replace"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return False

    if not isinstance(data, list):
        return False

    # An empty list is technically valid (no conversations)
    return all(isinstance(item, dict) for item in data)


_MISSING_TIMESTAMP_SENTINEL = datetime(2000, 1, 1, tzinfo=LA_TZ)
"""Fixed sentinel datetime used when a ChatGPT export has no timestamp."""


def _epoch_to_la_datetime(epoch: float | None) -> datetime:
    """Convert a Unix epoch float to a timezone-aware LA datetime.

    Returns a fixed sentinel value (``2000-01-01T00:00:00-08:00``)
    when *epoch* is ``None`` or ``0.0``, rather than using the
    current wall-clock time, to keep output deterministic.

    Args:
        epoch: Unix timestamp as a float, or None.

    Returns:
        A datetime in America/Los_Angeles timezone.
    """
    if epoch is None or epoch == 0.0:
        return _MISSING_TIMESTAMP_SENTINEL
    return datetime.fromtimestamp(epoch, tz=UTC).astimezone(LA_TZ)


def _linearize_tree(
    mapping: dict[str, Any],
) -> list[dict[str, Any]]:
    """Linearize a ChatGPT tree-structured mapping into an ordered message list.

    Finds the root node (parent is None), then walks the tree
    depth-first, always choosing the child branch with the most
    descendants when branching occurs.

    Args:
        mapping: The ``mapping`` dict from a ChatGPT conversation.

    Returns:
        An ordered list of message dicts (excluding null messages).
    """
    root_id = _find_root_id(mapping)
    if root_id is None:
        return []

    messages: list[dict[str, Any]] = []
    visited: set[str] = set()
    current_id: str | None = root_id

    while current_id is not None:
        if current_id in visited:
            logger.warning("Cycle detected at node %s; stopping traversal", current_id)
            break
        visited.add(current_id)

        node = mapping.get(current_id)
        if node is None:
            break

        msg = node.get("message")
        if msg is not None:
            messages.append(msg)

        children = node.get("children", [])
        current_id = _pick_longest_branch(children, mapping)

    return messages


def _find_root_id(mapping: dict[str, Any]) -> str | None:
    """Find the root node ID in a ChatGPT mapping (parent is None).

    Args:
        mapping: The ``mapping`` dict from a ChatGPT conversation.

    Returns:
        The ID of the root node, or None if not found.
    """
    for node_id, node in mapping.items():
        if node.get("parent") is None:
            return node_id
    return None


def _pick_longest_branch(
    children: list[str],
    mapping: dict[str, Any],
) -> str | None:
    """Choose the child node that leads to the longest branch.

    When a node has multiple children (branching conversation),
    selects the branch with the most total descendants.

    Args:
        children: List of child node IDs.
        mapping: The full mapping dict for counting descendants.

    Returns:
        The child ID leading to the longest branch, or None if no children.
    """
    if not children:
        return None
    if len(children) == 1:
        return children[0]

    best_child: str | None = None
    best_count = -1
    for child_id in children:
        count = _count_descendants(child_id, mapping)
        if count > best_count:
            best_count = count
            best_child = child_id
    return best_child


def _count_descendants(node_id: str, mapping: dict[str, Any]) -> int:
    """Count the total number of descendants of a node (iterative BFS).

    Uses an iterative breadth-first approach with a ``visited`` set to
    avoid infinite loops on malformed data and to handle arbitrarily
    deep trees without hitting Python's recursion limit.

    Args:
        node_id: The ID of the node to count descendants for.
        mapping: The full mapping dict.

    Returns:
        The total number of descendant nodes (including the node itself).
    """
    count = 0
    stack: list[str] = [node_id]
    visited: set[str] = set()

    while stack:
        nid = stack.pop()
        if nid in visited:
            continue
        visited.add(nid)
        node = mapping.get(nid)
        if node is None:
            continue
        count += 1
        stack.extend(node.get("children", []))

    return count


def _extract_message_text(msg: dict[str, Any]) -> str:
    """Extract the text content from a ChatGPT message dict.

    Joins all non-None string parts from the message's content.

    Args:
        msg: A ChatGPT message dict.

    Returns:
        The joined text content, or an empty string if no text found.
    """
    content = msg.get("content")
    if content is None:
        return ""
    parts = content.get("parts", [])
    text_parts = [str(p) for p in parts if p is not None]
    return "\n".join(text_parts)


def _get_message_role(msg: dict[str, Any]) -> str:
    """Extract the author role from a ChatGPT message dict.

    Args:
        msg: A ChatGPT message dict.

    Returns:
        The role string (e.g., 'user', 'assistant', 'system').
    """
    author = msg.get("author", {})
    return str(author.get("role", "unknown"))


def _epoch_to_authored_at(epoch: float | None) -> datetime | None:
    """Convert a ChatGPT ``create_time`` epoch to a UTC ``authored_at`` (FEAT-031).

    ChatGPT exports stamp every node with a ``create_time`` Unix
    epoch float. Per FEAT-031 ``authored_at`` is returned in UTC
    (no LA coercion) so cross-tz aggregations downstream see the
    instant the user actually sent the message, not its LA wall
    rendering. Missing / sentinel values return ``None`` — never
    a guessed date.
    """
    if epoch is None or epoch == 0.0:
        return None
    return datetime.fromtimestamp(epoch, tz=UTC)


def _pair_messages_to_fragments(
    messages: list[dict[str, Any]],
    title: str,
    conversation_timestamp: datetime,
    source_path: str,
    conversation_id: str | None = None,
    conversation_create_time: float | None = None,
) -> list[ParsedFragment]:
    """Pair consecutive user+assistant messages into fragments.

    Iterates through the ordered message list, pairing each user
    message with the following assistant message. System messages
    and unpaired user messages at the end are skipped. Each fragment
    title includes a turn index to prevent collisions.

    FEAT-031: each pair carries ``authored_at`` derived from the
    user message's ``create_time``. When the per-message field is
    absent we fall through to the conversation-level ``create_time``
    so a Substack-style "everything stamped at the conversation
    moment" export still anchors to a real source date instead of
    going to ``None``.

    Args:
        messages: Ordered list of ChatGPT message dicts.
        title: The conversation title.
        conversation_timestamp: The conversation creation timestamp.
        source_path: The source file path.
        conversation_id: Optional ChatGPT conversation ID for metadata.
        conversation_create_time: Raw epoch float from the conversation's
            ``create_time`` for the FEAT-031 ``authored_at`` fallback.

    Returns:
        A list of ``ParsedFragment`` objects, one per pair.
    """
    conv_authored_at = _epoch_to_authored_at(conversation_create_time)

    fragments: list[ParsedFragment] = []
    turn_idx = 0
    i = 0
    while i < len(messages):
        msg = messages[i]
        role = _get_message_role(msg)

        if role == "system":
            i += 1
            continue

        if role == "user":
            user_text = _extract_message_text(msg)
            user_time: float | None = msg.get("create_time")
            # Look for the next assistant message
            if i + 1 < len(messages):
                next_msg = messages[i + 1]
                next_role = _get_message_role(next_msg)
                if next_role == "assistant":
                    assistant_text = _extract_message_text(next_msg)
                    fragment_ts = (
                        _epoch_to_la_datetime(user_time)
                        if user_time
                        else conversation_timestamp
                    )
                    msg_authored_at = _epoch_to_authored_at(user_time)
                    authored_at = (
                        msg_authored_at
                        if msg_authored_at is not None
                        else conv_authored_at
                    )
                    content = (
                        f"**User**: {user_text}\n\n**Assistant**: {assistant_text}"
                    )
                    turn_title = f"{title} (turn {turn_idx})"
                    meta: dict[str, Any] = {
                        "title": turn_title,
                        "platform": "chatgpt",
                        "authored_at": authored_at,
                    }
                    if conversation_id is not None:
                        meta["conversation_id"] = conversation_id
                    fragments.append(
                        ParsedFragment(
                            content=content,
                            metadata=meta,
                            source_path=source_path,
                            timestamp=fragment_ts,
                        )
                    )
                    turn_idx += 1
                    i += 2
                    continue
            # No assistant follows: skip this user message
            i += 1
            continue

        # Skip any other role (e.g., tool)
        i += 1

    return fragments
