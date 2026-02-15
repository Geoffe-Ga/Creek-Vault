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

logger = logging.getLogger(__name__)


class ChatGPTIngestor(Ingestor):
    """Ingestor for ChatGPT JSON conversation exports.

    Handles the tree-structured ``mapping`` format used by ChatGPT's
    data export feature, where messages are connected via
    ``parent``/``children`` references.
    """

    def discover(self, source_path: Path) -> list[RawDocument]:
        """Find ChatGPT JSON export files in the given directory.

        Scans *source_path* for ``.json`` files and returns a
        ``RawDocument`` for each one.

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
        original file path.

        Args:
            fragment: The parsed fragment to generate frontmatter for.

        Returns:
            A dict of frontmatter key-value pairs.
        """
        return {
            "title": fragment.metadata.get("title", "Untitled Conversation"),
            "created": fragment.timestamp.isoformat(),
            "source": {
                "platform": "chatgpt",
                "original_file": fragment.source_path,
            },
        }

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

        ordered_messages = _linearize_tree(mapping)
        return _pair_messages_to_fragments(
            ordered_messages, title, timestamp, source_path
        )


def _epoch_to_la_datetime(epoch: float | None) -> datetime:
    """Convert a Unix epoch float to a timezone-aware LA datetime.

    Args:
        epoch: Unix timestamp as a float, or None.

    Returns:
        A datetime in America/Los_Angeles timezone.
    """
    if epoch is None or epoch == 0.0:
        return datetime.now(tz=LA_TZ)
    utc_dt = datetime.fromtimestamp(epoch, tz=UTC)
    return utc_dt.astimezone(LA_TZ)


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
    current_id: str | None = root_id

    while current_id is not None:
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
    """Count the total number of descendants of a node.

    Args:
        node_id: The ID of the node to count descendants for.
        mapping: The full mapping dict.

    Returns:
        The total number of descendant nodes (including the node itself).
    """
    node = mapping.get(node_id)
    if node is None:
        return 0
    count = 1
    for child_id in node.get("children", []):
        count += _count_descendants(child_id, mapping)
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
    role: str = author.get("role", "unknown")
    return role


def _pair_messages_to_fragments(
    messages: list[dict[str, Any]],
    title: str,
    conversation_timestamp: datetime,
    source_path: str,
) -> list[ParsedFragment]:
    """Pair consecutive user+assistant messages into fragments.

    Iterates through the ordered message list, pairing each user
    message with the following assistant message. System messages
    and unpaired user messages at the end are skipped.

    Args:
        messages: Ordered list of ChatGPT message dicts.
        title: The conversation title.
        conversation_timestamp: The conversation creation timestamp.
        source_path: The source file path.

    Returns:
        A list of ``ParsedFragment`` objects, one per pair.
    """
    fragments: list[ParsedFragment] = []
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
                    content = (
                        f"**User**: {user_text}\n\n**Assistant**: {assistant_text}"
                    )
                    fragments.append(
                        ParsedFragment(
                            content=content,
                            metadata={"title": title, "platform": "chatgpt"},
                            source_path=source_path,
                            timestamp=fragment_ts,
                        )
                    )
                    i += 2
                    continue
            # No assistant follows: skip this user message
            i += 1
            continue

        # Skip any other role (e.g., tool)
        i += 1

    return fragments
