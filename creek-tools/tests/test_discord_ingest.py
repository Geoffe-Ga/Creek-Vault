"""Tests for creek.ingest.discord — Discord message ingestor.

Covers discovery of channel directories, message parsing and grouping,
reply chain context, time-proximity grouping, markdown conversion,
frontmatter generation, edge cases (empty channels, missing fields,
custom emoji), and full pipeline integration.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from creek.ingest.base import ParsedFragment, RawDocument
from creek.ingest.discord import (
    DiscordIngestor,
    _build_message_index,
    _format_discord_content,
    _format_reply_context,
    _get_reference_id,
    _group_messages,
    _is_time_proximate,
    _parse_msg_timestamp,
    _safe_author_name,
    _safe_timestamp,
)

LA_TZ = ZoneInfo("America/Los_Angeles")


# ---- Fixture helpers ----


def _make_msg(
    msg_id: str = "msg-001",
    author: str = "Alice",
    content: str = "Hello world",
    timestamp: str = "2024-11-10T14:00:00Z",
    reference_id: str | None = None,
    embeds: list[dict[str, Any]] | None = None,
    reactions: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Create a Discord message dict for testing.

    Args:
        msg_id: The message ID.
        author: The author display name.
        content: The message text content.
        timestamp: ISO 8601 timestamp string.
        reference_id: Optional ID of the replied-to message.
        embeds: Optional list of embed dicts.
        reactions: Optional list of reaction dicts.

    Returns:
        A message dict matching Discord export format.
    """
    msg: dict[str, Any] = {
        "id": msg_id,
        "author": {"id": f"user-{author.lower()}", "name": author},
        "content": content,
        "timestamp": timestamp,
    }
    if reference_id is not None:
        msg["reference"] = {"messageId": reference_id}
    if embeds is not None:
        msg["embeds"] = embeds
    if reactions is not None:
        msg["reactions"] = reactions
    return msg


def _create_channel_dir(
    tmp_path: Path,
    channel_id: str = "123456",
    channel_name: str = "general",
    messages: list[dict[str, Any]] | None = None,
    channel_meta: dict[str, Any] | None = None,
    skip_channel_json: bool = False,
) -> Path:
    """Create a Discord channel directory with messages.json and channel.json.

    Args:
        tmp_path: The temporary directory root.
        channel_id: The channel directory name / ID.
        channel_name: The channel display name.
        messages: List of message dicts to write to messages.json.
        channel_meta: Optional channel metadata override.
        skip_channel_json: If True, do not create channel.json.

    Returns:
        The path to the created channel directory.
    """
    channel_dir = tmp_path / "messages" / channel_id
    channel_dir.mkdir(parents=True, exist_ok=True)

    if messages is None:
        messages = []
    messages_file = channel_dir / "messages.json"
    messages_file.write_text(json.dumps(messages), encoding="utf-8")

    if not skip_channel_json:
        if channel_meta is None:
            channel_meta = {
                "id": channel_id,
                "name": channel_name,
                "type": "text",
            }
        channel_file = channel_dir / "channel.json"
        channel_file.write_text(json.dumps(channel_meta), encoding="utf-8")

    return channel_dir


# ---- Helper function tests ----


class TestSafeAuthorName:
    """Tests for the _safe_author_name helper."""

    def test_normal_author(self) -> None:
        """Should extract author name from a well-formed message."""
        msg = _make_msg(author="Alice")
        assert _safe_author_name(msg) == "Alice"

    def test_missing_author_key(self) -> None:
        """Should return 'Unknown' when author key is missing."""
        msg: dict[str, Any] = {"id": "1", "content": "test"}
        assert _safe_author_name(msg) == "Unknown"

    def test_author_not_dict(self) -> None:
        """Should return 'Unknown' when author is not a dict."""
        msg: dict[str, Any] = {"id": "1", "author": "just-a-string"}
        assert _safe_author_name(msg) == "Unknown"

    def test_author_missing_name(self) -> None:
        """Should return 'Unknown' when author dict lacks name key."""
        msg: dict[str, Any] = {"id": "1", "author": {"id": "user-1"}}
        assert _safe_author_name(msg) == "Unknown"


class TestSafeTimestamp:
    """Tests for the _safe_timestamp helper."""

    def test_normal_timestamp(self) -> None:
        """Should extract timestamp from a well-formed message."""
        msg = _make_msg(timestamp="2024-11-10T14:00:00Z")
        assert _safe_timestamp(msg) == "2024-11-10T14:00:00Z"

    def test_missing_timestamp(self) -> None:
        """Should return empty string when timestamp is missing."""
        msg: dict[str, Any] = {"id": "1", "content": "test"}
        assert _safe_timestamp(msg) == ""


class TestParseMsgTimestamp:
    """Tests for the _parse_msg_timestamp helper."""

    def test_valid_iso_timestamp(self) -> None:
        """Should parse a valid ISO 8601 timestamp."""
        msg = _make_msg(timestamp="2024-11-10T14:00:00+00:00")
        result = _parse_msg_timestamp(msg)
        assert result is not None
        assert result.year == 2024
        assert result.month == 11

    def test_invalid_timestamp(self) -> None:
        """Should return None for an unparseable timestamp."""
        msg: dict[str, Any] = {"id": "1", "timestamp": "not-a-date"}
        assert _parse_msg_timestamp(msg) is None

    def test_missing_timestamp(self) -> None:
        """Should return None when timestamp is missing."""
        msg: dict[str, Any] = {"id": "1"}
        assert _parse_msg_timestamp(msg) is None


class TestGetReferenceId:
    """Tests for the _get_reference_id helper."""

    def test_with_reference(self) -> None:
        """Should extract messageId from reference dict."""
        msg = _make_msg(reference_id="msg-parent")
        assert _get_reference_id(msg) == "msg-parent"

    def test_without_reference(self) -> None:
        """Should return None when no reference key exists."""
        msg = _make_msg()
        assert _get_reference_id(msg) is None

    def test_reference_not_dict(self) -> None:
        """Should return None when reference is not a dict."""
        msg: dict[str, Any] = {"id": "1", "reference": "just-a-string"}
        assert _get_reference_id(msg) is None

    def test_reference_missing_message_id(self) -> None:
        """Should return None when reference dict lacks messageId."""
        msg: dict[str, Any] = {"id": "1", "reference": {"channelId": "ch-1"}}
        assert _get_reference_id(msg) is None


class TestBuildMessageIndex:
    """Tests for the _build_message_index helper."""

    def test_builds_index(self) -> None:
        """Should build a dict mapping message IDs to message dicts."""
        messages = [_make_msg(msg_id="a"), _make_msg(msg_id="b")]
        index = _build_message_index(messages)
        assert "a" in index
        assert "b" in index
        assert index["a"]["id"] == "a"

    def test_skips_messages_without_id(self) -> None:
        """Should skip messages that lack an id key."""
        messages: list[dict[str, Any]] = [
            {"content": "no id"},
            _make_msg(msg_id="valid"),
        ]
        index = _build_message_index(messages)
        assert len(index) == 1
        assert "valid" in index

    def test_empty_list(self) -> None:
        """Should return empty dict for empty message list."""
        assert _build_message_index([]) == {}


class TestFormatDiscordContent:
    """Tests for the _format_discord_content helper."""

    def test_spoiler_tags(self) -> None:
        """Should convert spoiler tags to readable format."""
        result = _format_discord_content("This is ||spoiler text|| here")
        assert result == "This is [SPOILER: spoiler text] here"

    def test_multiple_spoilers(self) -> None:
        """Should convert multiple spoiler tags."""
        result = _format_discord_content("||a|| and ||b||")
        assert result == "[SPOILER: a] and [SPOILER: b]"

    def test_no_formatting(self) -> None:
        """Should return plain text unchanged."""
        result = _format_discord_content("plain text")
        assert result == "plain text"

    def test_empty_string(self) -> None:
        """Should handle empty string input."""
        assert _format_discord_content("") == ""


class TestFormatReplyContext:
    """Tests for the _format_reply_context helper."""

    def test_formats_reply(self) -> None:
        """Should format parent message as a blockquote with author."""
        parent = _make_msg(author="Bob", content="Original message")
        result = _format_reply_context(parent)
        assert result == "> **Bob**: Original message"

    def test_missing_content(self) -> None:
        """Should handle parent with missing content."""
        parent: dict[str, Any] = {"author": {"name": "Bob"}}
        result = _format_reply_context(parent)
        assert result == "> **Bob**: "


class TestIsTimeProximate:
    """Tests for the _is_time_proximate helper."""

    def test_same_author_within_threshold(self) -> None:
        """Should return True for same author within 5 minutes."""
        msg1 = _make_msg(author="Alice", timestamp="2024-11-10T14:00:00Z")
        msg2 = _make_msg(author="Alice", timestamp="2024-11-10T14:03:00Z")
        assert _is_time_proximate(msg2, msg1) is True

    def test_same_author_at_boundary(self) -> None:
        """Should return True for same author exactly at 5-minute boundary."""
        msg1 = _make_msg(author="Alice", timestamp="2024-11-10T14:00:00Z")
        msg2 = _make_msg(author="Alice", timestamp="2024-11-10T14:05:00Z")
        assert _is_time_proximate(msg2, msg1) is True

    def test_same_author_over_threshold(self) -> None:
        """Should return False for same author beyond 5 minutes."""
        msg1 = _make_msg(author="Alice", timestamp="2024-11-10T14:00:00Z")
        msg2 = _make_msg(author="Alice", timestamp="2024-11-10T14:06:00Z")
        assert _is_time_proximate(msg2, msg1) is False

    def test_different_author_within_threshold(self) -> None:
        """Should return False for different authors even within threshold."""
        msg1 = _make_msg(author="Alice", timestamp="2024-11-10T14:00:00Z")
        msg2 = _make_msg(author="Bob", timestamp="2024-11-10T14:01:00Z")
        assert _is_time_proximate(msg2, msg1) is False

    def test_missing_timestamps(self) -> None:
        """Should return False when timestamps cannot be parsed."""
        msg1: dict[str, Any] = {"author": {"name": "Alice"}}
        msg2: dict[str, Any] = {"author": {"name": "Alice"}}
        assert _is_time_proximate(msg2, msg1) is False


# ---- Grouping tests ----


class TestGroupMessages:
    """Tests for the _group_messages function."""

    def test_single_message(self) -> None:
        """Single message should form one group."""
        messages = [_make_msg()]
        groups = _group_messages(messages)
        assert len(groups) == 1
        assert len(groups[0]) == 1

    def test_time_proximity_grouping(self) -> None:
        """Messages from same author within 5 min should group together."""
        messages = [
            _make_msg(msg_id="1", author="Alice", timestamp="2024-11-10T14:00:00Z"),
            _make_msg(msg_id="2", author="Alice", timestamp="2024-11-10T14:02:00Z"),
            _make_msg(msg_id="3", author="Alice", timestamp="2024-11-10T14:04:00Z"),
        ]
        groups = _group_messages(messages)
        assert len(groups) == 1
        assert len(groups[0]) == 3

    def test_different_authors_separate_groups(self) -> None:
        """Messages from different authors should form separate groups."""
        messages = [
            _make_msg(msg_id="1", author="Alice", timestamp="2024-11-10T14:00:00Z"),
            _make_msg(msg_id="2", author="Bob", timestamp="2024-11-10T14:01:00Z"),
        ]
        groups = _group_messages(messages)
        assert len(groups) == 2

    def test_reply_chain_grouping(self) -> None:
        """Reply to a message in the current group should stay in group."""
        messages = [
            _make_msg(msg_id="1", author="Alice", timestamp="2024-11-10T14:00:00Z"),
            _make_msg(
                msg_id="2",
                author="Bob",
                timestamp="2024-11-10T14:01:00Z",
                reference_id="1",
            ),
        ]
        groups = _group_messages(messages)
        assert len(groups) == 1
        assert len(groups[0]) == 2

    def test_time_gap_creates_new_group(self) -> None:
        """Messages separated by more than 5 min from same author split."""
        messages = [
            _make_msg(msg_id="1", author="Alice", timestamp="2024-11-10T14:00:00Z"),
            _make_msg(msg_id="2", author="Alice", timestamp="2024-11-10T14:10:00Z"),
        ]
        groups = _group_messages(messages)
        assert len(groups) == 2

    def test_empty_messages(self) -> None:
        """Empty message list should return empty groups."""
        assert _group_messages([]) == []

    def test_mixed_conversation(self) -> None:
        """Mixed conversation with replies and time gaps groups correctly."""
        messages = [
            _make_msg(
                msg_id="1",
                author="Alice",
                content="Question?",
                timestamp="2024-11-10T14:00:00Z",
            ),
            _make_msg(
                msg_id="2",
                author="Bob",
                content="Answer!",
                timestamp="2024-11-10T14:01:00Z",
                reference_id="1",
            ),
            _make_msg(
                msg_id="3",
                author="Charlie",
                content="New topic",
                timestamp="2024-11-10T15:00:00Z",
            ),
        ]
        groups = _group_messages(messages)
        assert len(groups) == 2
        assert len(groups[0]) == 2  # Alice + Bob reply
        assert len(groups[1]) == 1  # Charlie's new topic


# ---- DiscordIngestor.discover() tests ----


class TestDiscordIngestorDiscover:
    """Tests for DiscordIngestor.discover()."""

    def test_discover_finds_channels(self, tmp_path: Path) -> None:
        """Should find messages.json files in channel directories."""
        _create_channel_dir(
            tmp_path,
            channel_id="ch-1",
            channel_name="general",
            messages=[_make_msg()],
        )
        ingestor = DiscordIngestor()
        docs = ingestor.discover(tmp_path)
        assert len(docs) == 1
        assert docs[0].path.name == "messages.json"

    def test_discover_multiple_channels(self, tmp_path: Path) -> None:
        """Should find messages.json in multiple channel directories."""
        _create_channel_dir(tmp_path, channel_id="ch-1", channel_name="general")
        _create_channel_dir(tmp_path, channel_id="ch-2", channel_name="random")
        ingestor = DiscordIngestor()
        docs = ingestor.discover(tmp_path)
        assert len(docs) == 2

    def test_discover_no_messages_dir(self, tmp_path: Path) -> None:
        """Should return empty list when messages/ directory doesn't exist."""
        ingestor = DiscordIngestor()
        docs = ingestor.discover(tmp_path)
        assert docs == []

    def test_discover_empty_messages_dir(self, tmp_path: Path) -> None:
        """Should return empty list when messages/ has no channel dirs."""
        (tmp_path / "messages").mkdir()
        ingestor = DiscordIngestor()
        docs = ingestor.discover(tmp_path)
        assert docs == []

    def test_discover_skips_non_directories(self, tmp_path: Path) -> None:
        """Should skip non-directory entries in messages/."""
        messages_dir = tmp_path / "messages"
        messages_dir.mkdir()
        (messages_dir / "stray_file.txt").write_text("not a channel")
        ingestor = DiscordIngestor()
        docs = ingestor.discover(tmp_path)
        assert docs == []

    def test_discover_skips_channel_without_messages_json(self, tmp_path: Path) -> None:
        """Should skip channel dirs that lack messages.json."""
        channel_dir = tmp_path / "messages" / "ch-1"
        channel_dir.mkdir(parents=True)
        (channel_dir / "channel.json").write_text("{}")
        ingestor = DiscordIngestor()
        docs = ingestor.discover(tmp_path)
        assert docs == []

    def test_discover_loads_channel_metadata(self, tmp_path: Path) -> None:
        """Should populate metadata from channel.json."""
        _create_channel_dir(
            tmp_path,
            channel_id="ch-1",
            channel_name="knowledge-sharing",
            messages=[_make_msg()],
        )
        ingestor = DiscordIngestor()
        docs = ingestor.discover(tmp_path)
        assert docs[0].metadata["channel_name"] == "knowledge-sharing"
        assert docs[0].metadata["channel_id"] == "ch-1"

    def test_discover_missing_channel_json(self, tmp_path: Path) -> None:
        """Should use directory name as fallback when channel.json is absent."""
        _create_channel_dir(
            tmp_path,
            channel_id="ch-fallback",
            messages=[_make_msg()],
            skip_channel_json=True,
        )
        ingestor = DiscordIngestor()
        docs = ingestor.discover(tmp_path)
        assert docs[0].metadata["channel_name"] == "ch-fallback"
        assert docs[0].metadata["channel_type"] == "unknown"

    def test_discover_malformed_channel_json(self, tmp_path: Path) -> None:
        """Should fall back to defaults when channel.json is malformed."""
        channel_dir = tmp_path / "messages" / "ch-bad"
        channel_dir.mkdir(parents=True)
        (channel_dir / "messages.json").write_text("[]")
        (channel_dir / "channel.json").write_text("not valid json {{{")
        ingestor = DiscordIngestor()
        docs = ingestor.discover(tmp_path)
        assert docs[0].metadata["channel_name"] == "ch-bad"


# ---- DiscordIngestor.parse() tests ----


class TestDiscordIngestorParse:
    """Tests for DiscordIngestor.parse()."""

    def _raw_doc(
        self,
        messages: list[dict[str, Any]],
        channel_name: str = "general",
    ) -> RawDocument:
        """Create a RawDocument from a message list.

        Args:
            messages: List of message dicts.
            channel_name: Channel name for metadata.

        Returns:
            A RawDocument with the serialized messages as content.
        """
        return RawDocument(
            path=Path("/fake/messages.json"),
            content=json.dumps(messages).encode("utf-8"),
            metadata={
                "channel_name": channel_name,
                "channel_id": "ch-1",
                "channel_type": "text",
            },
            detected_encoding="utf-8",
        )

    def test_parse_single_message(self) -> None:
        """Should create one fragment from a single message."""
        messages = [_make_msg()]
        ingestor = DiscordIngestor()
        fragments = ingestor.parse(self._raw_doc(messages))
        assert len(fragments) == 1
        assert "Alice" in fragments[0].content
        assert "Hello world" in fragments[0].content

    def test_parse_grouped_messages(self) -> None:
        """Should group time-proximate messages into one fragment."""
        messages = [
            _make_msg(msg_id="1", author="Alice", timestamp="2024-11-10T14:00:00Z"),
            _make_msg(
                msg_id="2",
                author="Alice",
                content="Follow-up",
                timestamp="2024-11-10T14:02:00Z",
            ),
        ]
        ingestor = DiscordIngestor()
        fragments = ingestor.parse(self._raw_doc(messages))
        assert len(fragments) == 1
        assert "Follow-up" in fragments[0].content

    def test_parse_multiple_groups(self) -> None:
        """Should create separate fragments for different groups."""
        messages = [
            _make_msg(msg_id="1", author="Alice", timestamp="2024-11-10T14:00:00Z"),
            _make_msg(msg_id="2", author="Bob", timestamp="2024-11-10T15:00:00Z"),
        ]
        ingestor = DiscordIngestor()
        fragments = ingestor.parse(self._raw_doc(messages))
        assert len(fragments) == 2

    def test_parse_reply_includes_context(self) -> None:
        """Reply messages should include quoted parent context."""
        messages = [
            _make_msg(
                msg_id="1",
                author="Alice",
                content="Original",
                timestamp="2024-11-10T14:00:00Z",
            ),
            _make_msg(
                msg_id="2",
                author="Bob",
                content="Reply",
                timestamp="2024-11-10T14:01:00Z",
                reference_id="1",
            ),
        ]
        ingestor = DiscordIngestor()
        fragments = ingestor.parse(self._raw_doc(messages))
        assert len(fragments) == 1
        content = fragments[0].content
        assert "> **Alice**: Original" in content
        assert "**Bob**: Reply" in content

    def test_parse_empty_messages(self) -> None:
        """Should return no fragments for empty message list."""
        ingestor = DiscordIngestor()
        fragments = ingestor.parse(self._raw_doc([]))
        assert fragments == []

    def test_parse_invalid_json(self) -> None:
        """Should return no fragments when messages.json is not valid JSON."""
        raw = RawDocument(
            path=Path("/fake/messages.json"),
            content=b"not json {{{",
            metadata={"channel_name": "general", "channel_id": "ch-1"},
            detected_encoding="utf-8",
        )
        ingestor = DiscordIngestor()
        fragments = ingestor.parse(raw)
        assert fragments == []

    def test_parse_object_format_with_messages_key(self) -> None:
        """Should handle object format with a 'messages' key."""
        data = {"messages": [_make_msg()]}
        raw = RawDocument(
            path=Path("/fake/messages.json"),
            content=json.dumps(data).encode("utf-8"),
            metadata={"channel_name": "general", "channel_id": "ch-1"},
            detected_encoding="utf-8",
        )
        ingestor = DiscordIngestor()
        fragments = ingestor.parse(raw)
        assert len(fragments) == 1

    def test_parse_preserves_metadata(self) -> None:
        """Should populate fragment metadata with channel and author info."""
        messages = [_make_msg(author="Alice")]
        ingestor = DiscordIngestor()
        fragments = ingestor.parse(self._raw_doc(messages, channel_name="test-chan"))
        frag = fragments[0]
        assert frag.metadata["channel_name"] == "test-chan"
        assert "Alice" in frag.metadata["authors"]
        assert frag.metadata["message_count"] == 1

    def test_parse_message_with_embeds(self) -> None:
        """Should format embed content in parsed output."""
        messages = [
            _make_msg(
                embeds=[
                    {
                        "title": "Cool Link",
                        "description": "A description",
                        "url": "https://example.com",
                    }
                ],
            ),
        ]
        ingestor = DiscordIngestor()
        fragments = ingestor.parse(self._raw_doc(messages))
        content = fragments[0].content
        assert "Embed: Cool Link" in content
        assert "A description" in content
        assert "https://example.com" in content

    def test_parse_message_with_reactions(self) -> None:
        """Should format reactions in parsed output."""
        messages = [
            _make_msg(
                reactions=[
                    {"emoji": {"name": "thumbsup"}, "count": 3},
                    {"emoji": {"name": "heart"}, "count": 1},
                ],
            ),
        ]
        ingestor = DiscordIngestor()
        fragments = ingestor.parse(self._raw_doc(messages))
        content = fragments[0].content
        assert "thumbsup x3" in content
        assert "heart x1" in content

    def test_parse_message_with_spoilers(self) -> None:
        """Should convert spoiler formatting in parsed output."""
        messages = [
            _make_msg(content="This is ||secret|| content"),
        ]
        ingestor = DiscordIngestor()
        fragments = ingestor.parse(self._raw_doc(messages))
        assert "[SPOILER: secret]" in fragments[0].content

    def test_parse_timestamp_normalization(self) -> None:
        """Should normalize timestamp to configured timezone."""
        messages = [
            _make_msg(timestamp="2024-11-10T20:00:00+00:00"),
        ]
        ingestor = DiscordIngestor()
        fragments = ingestor.parse(self._raw_doc(messages))
        ts = fragments[0].timestamp
        assert str(ts.tzinfo) == "America/Los_Angeles"

    def test_parse_missing_message_fields(self) -> None:
        """Should handle messages with missing fields gracefully."""
        messages: list[dict[str, Any]] = [
            {"id": "1", "timestamp": "2024-11-10T14:00:00Z"},
        ]
        ingestor = DiscordIngestor()
        fragments = ingestor.parse(self._raw_doc(messages))
        # Should still produce a fragment (with Unknown author, empty content)
        assert len(fragments) == 1

    def test_parse_object_format_without_messages_key(self) -> None:
        """Should return empty list for object format without messages key."""
        data = {"something_else": []}
        raw = RawDocument(
            path=Path("/fake/messages.json"),
            content=json.dumps(data).encode("utf-8"),
            metadata={"channel_name": "general", "channel_id": "ch-1"},
            detected_encoding="utf-8",
        )
        ingestor = DiscordIngestor()
        fragments = ingestor.parse(raw)
        assert fragments == []

    def test_parse_custom_emoji_in_reactions(self) -> None:
        """Should handle custom emoji format in reactions."""
        messages = [
            _make_msg(
                reactions=[
                    {"emoji": {"name": "custom_emoji", "id": "12345"}, "count": 2},
                ],
            ),
        ]
        ingestor = DiscordIngestor()
        fragments = ingestor.parse(self._raw_doc(messages))
        assert "custom_emoji x2" in fragments[0].content

    def test_parse_embed_without_title(self) -> None:
        """Should handle embeds that lack a title."""
        messages = [
            _make_msg(
                embeds=[{"description": "Just a description"}],
            ),
        ]
        ingestor = DiscordIngestor()
        fragments = ingestor.parse(self._raw_doc(messages))
        content = fragments[0].content
        assert "Just a description" in content

    def test_parse_non_dict_embed(self) -> None:
        """Should skip embeds that are not dicts."""
        messages = [
            _make_msg(embeds=["not-a-dict"]),
        ]
        ingestor = DiscordIngestor()
        fragments = ingestor.parse(self._raw_doc(messages))
        # Should still produce a fragment without embed text
        assert len(fragments) == 1

    def test_parse_non_dict_reaction(self) -> None:
        """Should skip reactions that are not dicts."""
        messages = [
            _make_msg(reactions=["not-a-dict"]),
        ]
        ingestor = DiscordIngestor()
        fragments = ingestor.parse(self._raw_doc(messages))
        # Should still produce a fragment (reactions line skipped)
        assert len(fragments) == 1

    def test_parse_emoji_not_dict(self) -> None:
        """Should handle emoji field that is not a dict."""
        messages = [
            _make_msg(
                reactions=[{"emoji": "simple_emoji_string", "count": 1}],
            ),
        ]
        ingestor = DiscordIngestor()
        fragments = ingestor.parse(self._raw_doc(messages))
        assert "simple_emoji_string x1" in fragments[0].content

    def test_parse_messages_key_not_list(self) -> None:
        """Should return empty when messages key is not a list."""
        data = {"messages": "not-a-list"}
        raw = RawDocument(
            path=Path("/fake/messages.json"),
            content=json.dumps(data).encode("utf-8"),
            metadata={"channel_name": "general", "channel_id": "ch-1"},
            detected_encoding="utf-8",
        )
        ingestor = DiscordIngestor()
        fragments = ingestor.parse(raw)
        assert fragments == []


# ---- DiscordIngestor.convert_to_markdown() tests ----


class TestDiscordIngestorConvertToMarkdown:
    """Tests for DiscordIngestor.convert_to_markdown()."""

    def _fragment(
        self,
        content: str = "message content",
        channel_name: str = "general",
    ) -> ParsedFragment:
        """Create a ParsedFragment for testing.

        Args:
            content: The fragment content.
            channel_name: The channel name in metadata.

        Returns:
            A ParsedFragment with the given content and channel.
        """
        return ParsedFragment(
            content=content,
            metadata={"channel_name": channel_name},
            source_path="/fake/messages.json",
            timestamp=datetime(2024, 11, 10, 14, 0, 0, tzinfo=LA_TZ),
        )

    def test_includes_channel_header(self) -> None:
        """Should include channel name as H1 header."""
        ingestor = DiscordIngestor()
        md = ingestor.convert_to_markdown(self._fragment(channel_name="general"))
        assert md.startswith("# #general\n\n")

    def test_includes_content(self) -> None:
        """Should include the fragment content after the header."""
        ingestor = DiscordIngestor()
        md = ingestor.convert_to_markdown(
            self._fragment(content="Hello world", channel_name="test")
        )
        assert "Hello world" in md

    def test_missing_channel_name(self) -> None:
        """Should use 'unknown' when channel_name is missing from metadata."""
        frag = ParsedFragment(
            content="content",
            metadata={},
            source_path="/fake/messages.json",
            timestamp=datetime(2024, 11, 10, 14, 0, 0, tzinfo=LA_TZ),
        )
        ingestor = DiscordIngestor()
        md = ingestor.convert_to_markdown(frag)
        assert "# #unknown" in md


# ---- DiscordIngestor.generate_frontmatter() tests ----


class TestDiscordIngestorGenerateFrontmatter:
    """Tests for DiscordIngestor.generate_frontmatter()."""

    def _fragment(self) -> ParsedFragment:
        """Create a ParsedFragment with typical Discord metadata.

        Returns:
            A ParsedFragment for testing frontmatter generation.
        """
        return ParsedFragment(
            content="test content",
            metadata={
                "channel_name": "knowledge-sharing",
                "channel_id": "ch-123",
                "authors": ["Alice", "Bob"],
                "message_count": 3,
            },
            source_path="/fake/messages.json",
            timestamp=datetime(2024, 11, 10, 14, 0, 0, tzinfo=LA_TZ),
        )

    def test_source_platform(self) -> None:
        """Should set source.platform to 'discord'."""
        ingestor = DiscordIngestor()
        fm = ingestor.generate_frontmatter(self._fragment())
        assert fm["source"]["platform"] == "discord"

    def test_source_channel(self) -> None:
        """Should set source.channel from metadata."""
        ingestor = DiscordIngestor()
        fm = ingestor.generate_frontmatter(self._fragment())
        assert fm["source"]["channel"] == "knowledge-sharing"

    def test_source_channel_id(self) -> None:
        """Should set source.channel_id from metadata."""
        ingestor = DiscordIngestor()
        fm = ingestor.generate_frontmatter(self._fragment())
        assert fm["source"]["channel_id"] == "ch-123"

    def test_created_timestamp(self) -> None:
        """Should include an ISO 8601 created timestamp."""
        ingestor = DiscordIngestor()
        fm = ingestor.generate_frontmatter(self._fragment())
        assert "created" in fm
        # Should be parseable as ISO 8601
        datetime.fromisoformat(fm["created"])

    def test_authors_list(self) -> None:
        """Should include sorted authors list."""
        ingestor = DiscordIngestor()
        fm = ingestor.generate_frontmatter(self._fragment())
        assert fm["authors"] == ["Alice", "Bob"]

    def test_message_count(self) -> None:
        """Should include message count."""
        ingestor = DiscordIngestor()
        fm = ingestor.generate_frontmatter(self._fragment())
        assert fm["message_count"] == 3

    def test_missing_metadata_uses_defaults(self) -> None:
        """Should use default values when metadata keys are missing."""
        frag = ParsedFragment(
            content="test",
            metadata={},
            source_path="/fake/messages.json",
            timestamp=datetime(2024, 11, 10, 14, 0, 0, tzinfo=LA_TZ),
        )
        ingestor = DiscordIngestor()
        fm = ingestor.generate_frontmatter(frag)
        assert fm["source"]["channel"] == "unknown"
        assert fm["source"]["channel_id"] == "unknown"
        assert fm["authors"] == []
        assert fm["message_count"] == 0


# ---- Registry tests ----


class TestDiscordIngestorRegistry:
    """Tests for DiscordIngestor registration in INGESTOR_REGISTRY."""

    def test_registered(self) -> None:
        """DiscordIngestor should be registered as 'discord'."""
        from creek.ingest import INGESTOR_REGISTRY

        assert "discord" in INGESTOR_REGISTRY
        assert INGESTOR_REGISTRY["discord"] is DiscordIngestor

    def test_is_ingestor_subclass(self) -> None:
        """DiscordIngestor should be a subclass of Ingestor."""
        from creek.ingest.base import Ingestor

        assert issubclass(DiscordIngestor, Ingestor)


# ---- Full pipeline integration test ----


class TestDiscordIngestorPipeline:
    """Integration tests for the full DiscordIngestor pipeline."""

    def test_full_pipeline(self, tmp_path: Path) -> None:
        """Full ingest pipeline should discover, parse, and produce results."""
        messages = [
            _make_msg(
                msg_id="1",
                author="Alice",
                content="Has anyone tried Obsidian?",
                timestamp="2024-11-10T14:00:00Z",
            ),
            _make_msg(
                msg_id="2",
                author="Bob",
                content="Yes, the linking is powerful.",
                timestamp="2024-11-10T14:02:00Z",
                reference_id="1",
            ),
            _make_msg(
                msg_id="3",
                author="Alice",
                content="The graph view sounds interesting.",
                timestamp="2024-11-10T14:05:00Z",
            ),
        ]
        _create_channel_dir(
            tmp_path,
            channel_id="ch-1",
            channel_name="knowledge-sharing",
            messages=messages,
        )

        ingestor = DiscordIngestor()
        result = ingestor.ingest(tmp_path)

        assert len(result.errors) == 0
        assert len(result.fragments) > 0
        assert len(result.provenance) > 0

        # Verify provenance
        for prov in result.provenance:
            assert prov.status == "success"
            assert prov.ingestor_name == "DiscordIngestor"

    def test_pipeline_multi_channel(self, tmp_path: Path) -> None:
        """Pipeline should process fragments from multiple channels."""
        _create_channel_dir(
            tmp_path,
            channel_id="ch-1",
            channel_name="general",
            messages=[_make_msg(msg_id="1", author="Alice")],
        )
        _create_channel_dir(
            tmp_path,
            channel_id="ch-2",
            channel_name="random",
            messages=[_make_msg(msg_id="2", author="Bob")],
        )

        ingestor = DiscordIngestor()
        result = ingestor.ingest(tmp_path)

        assert len(result.errors) == 0
        assert len(result.fragments) == 2

    def test_pipeline_empty_export(self, tmp_path: Path) -> None:
        """Pipeline should handle an empty export gracefully."""
        ingestor = DiscordIngestor()
        result = ingestor.ingest(tmp_path)
        assert result.fragments == []
        assert result.errors == []

    def test_pipeline_empty_channel(self, tmp_path: Path) -> None:
        """Pipeline should handle a channel with no messages."""
        _create_channel_dir(
            tmp_path,
            channel_id="ch-empty",
            channel_name="empty-channel",
            messages=[],
        )
        ingestor = DiscordIngestor()
        result = ingestor.ingest(tmp_path)
        assert result.fragments == []
        assert result.errors == []

    def test_pipeline_fragment_markdown_and_frontmatter(self, tmp_path: Path) -> None:
        """Fragments should have markdown and frontmatter in metadata."""
        _create_channel_dir(
            tmp_path,
            channel_id="ch-1",
            channel_name="test",
            messages=[_make_msg(content="Test message")],
        )
        ingestor = DiscordIngestor()
        result = ingestor.ingest(tmp_path)
        frag = result.fragments[0]

        assert "markdown" in frag.metadata
        assert "# #test" in frag.metadata["markdown"]
        assert "frontmatter" in frag.metadata
        assert frag.metadata["frontmatter"]["source"]["platform"] == "discord"

    def test_resolve_timestamp_fallback(self) -> None:
        """Should fall back to epoch for empty or invalid timestamps."""
        ingestor = DiscordIngestor()
        ts = ingestor._resolve_timestamp("")
        assert ts.year == 1969 or ts.year == 1970  # depends on LA offset

        ts2 = ingestor._resolve_timestamp("not-a-date")
        assert ts2.year == 1969 or ts2.year == 1970


class TestDiscordIngestorEdgeCases:
    """Edge case tests for DiscordIngestor."""

    def test_reply_to_message_outside_group(self) -> None:
        """Reply to a message not in current group should start new group."""
        messages = [
            _make_msg(msg_id="1", author="Alice", timestamp="2024-11-10T14:00:00Z"),
            _make_msg(
                msg_id="2",
                author="Bob",
                content="Unrelated",
                timestamp="2024-11-10T15:00:00Z",
            ),
            _make_msg(
                msg_id="3",
                author="Charlie",
                content="Reply to old msg",
                timestamp="2024-11-10T15:01:00Z",
                reference_id="1",
            ),
        ]
        groups = _group_messages(messages)
        # msg 1 alone, then msg 2 + msg 3 (msg 3 replies to msg 1
        # which is NOT in the current group, so it doesn't join;
        # but it's different author from Bob, so separate group)
        assert len(groups) == 3

    def test_message_ids_in_fragment_metadata(self) -> None:
        """Fragment metadata should contain message IDs."""
        messages = [_make_msg(msg_id="msg-42")]
        raw = RawDocument(
            path=Path("/fake/messages.json"),
            content=json.dumps(messages).encode("utf-8"),
            metadata={"channel_name": "general", "channel_id": "ch-1"},
            detected_encoding="utf-8",
        )
        ingestor = DiscordIngestor()
        fragments = ingestor.parse(raw)
        assert "msg-42" in fragments[0].metadata["message_ids"]

    def test_embed_with_only_url(self) -> None:
        """Should format embed with only a URL field."""
        messages = [
            _make_msg(embeds=[{"url": "https://example.com"}]),
        ]
        raw = RawDocument(
            path=Path("/fake/messages.json"),
            content=json.dumps(messages).encode("utf-8"),
            metadata={"channel_name": "general", "channel_id": "ch-1"},
            detected_encoding="utf-8",
        )
        ingestor = DiscordIngestor()
        fragments = ingestor.parse(raw)
        assert "https://example.com" in fragments[0].content

    def test_empty_reactions_list(self) -> None:
        """Should handle empty reactions list without adding reactions line."""
        messages = [_make_msg(reactions=[])]
        raw = RawDocument(
            path=Path("/fake/messages.json"),
            content=json.dumps(messages).encode("utf-8"),
            metadata={"channel_name": "general", "channel_id": "ch-1"},
            detected_encoding="utf-8",
        )
        ingestor = DiscordIngestor()
        fragments = ingestor.parse(raw)
        assert "Reactions:" not in fragments[0].content
