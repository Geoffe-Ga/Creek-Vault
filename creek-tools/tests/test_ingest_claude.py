"""Tests for creek.ingest.claude — Claude conversation JSON ingestor.

Covers discovery of Claude export JSON files, parsing conversation turns
into fragments, Markdown conversion with blockquote formatting, frontmatter
generation with platform metadata, INGESTOR_REGISTRY registration, and
edge cases (empty conversations, missing fields, system prompts).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from creek.ingest.base import Ingestor, IngestResult, ParsedFragment, RawDocument
from creek.ingest.claude import ClaudeIngestor

LA_TZ = ZoneInfo("America/Los_Angeles")

# ---- Fixture Helpers ----


def _make_claude_export(conversations: list[dict[str, Any]]) -> bytes:
    """Build a Claude export JSON blob from a list of conversation dicts.

    Args:
        conversations: List of conversation dicts with uuid, name,
            created_at, and messages fields.

    Returns:
        UTF-8 encoded JSON bytes.
    """
    return json.dumps({"conversations": conversations}).encode("utf-8")


def _make_single_conversation(
    *,
    uuid: str = "conv-001",
    name: str = "Test Conversation",
    created_at: str = "2024-11-15T10:30:00Z",
    model: str = "claude-3-opus-20240229",
    messages: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build a single conversation dict for test fixtures.

    Args:
        uuid: Conversation UUID.
        name: Conversation title.
        created_at: ISO timestamp for conversation creation.
        model: Model slug used in the conversation.
        messages: List of message dicts; defaults to a two-turn exchange.

    Returns:
        A conversation dict suitable for embedding in a Claude export.
    """
    if messages is None:
        messages = [
            {
                "role": "human",
                "content": "How should I organize knowledge?",
                "created_at": "2024-11-15T10:30:00Z",
            },
            {
                "role": "assistant",
                "content": "Use a system that mirrors how you think.",
                "created_at": "2024-11-15T10:30:15Z",
            },
        ]
    conv: dict[str, Any] = {
        "uuid": uuid,
        "name": name,
        "created_at": created_at,
        "messages": messages,
    }
    if model:
        conv["model"] = model
    return conv


def _raw_doc_from_export(
    export_bytes: bytes,
    path: str = "/fake/export/claude_export.json",
) -> RawDocument:
    """Wrap export bytes in a RawDocument for parse() calls.

    Args:
        export_bytes: The raw JSON bytes.
        path: Fake file path for the document.

    Returns:
        A RawDocument wrapping the given bytes.
    """
    return RawDocument(
        path=Path(path),
        content=export_bytes,
        metadata={},
        detected_encoding="utf-8",
    )


# ---- Fixtures ----


@pytest.fixture()
def ingestor() -> ClaudeIngestor:
    """Return a fresh ClaudeIngestor instance.

    Returns:
        A ClaudeIngestor ready for testing.
    """
    return ClaudeIngestor()


@pytest.fixture()
def sample_export_dir(tmp_path: Path) -> Path:
    """Create a temp directory containing a valid Claude export JSON file.

    Args:
        tmp_path: Pytest-provided temporary directory.

    Returns:
        Path to the temporary directory with the export file.
    """
    conv = _make_single_conversation()
    data = _make_claude_export([conv])
    export_file = tmp_path / "claude_export.json"
    export_file.write_bytes(data)
    return tmp_path


@pytest.fixture()
def sample_raw_doc() -> RawDocument:
    """Return a RawDocument containing a minimal valid Claude export.

    Returns:
        A RawDocument with a single two-turn conversation.
    """
    conv = _make_single_conversation()
    return _raw_doc_from_export(_make_claude_export([conv]))


# ---- ClaudeIngestor Class Tests ----


class TestClaudeIngestorClass:
    """Tests for ClaudeIngestor class structure and registration."""

    def test_is_subclass_of_ingestor(self) -> None:
        """ClaudeIngestor should be a subclass of Ingestor."""
        assert issubclass(ClaudeIngestor, Ingestor)

    def test_can_instantiate(self) -> None:
        """ClaudeIngestor should be instantiable."""
        ingestor = ClaudeIngestor()
        assert isinstance(ingestor, ClaudeIngestor)

    def test_registered_in_registry(self) -> None:
        """ClaudeIngestor should be registered in INGESTOR_REGISTRY."""
        from creek.ingest import INGESTOR_REGISTRY

        assert "claude" in INGESTOR_REGISTRY
        assert INGESTOR_REGISTRY["claude"] is ClaudeIngestor


# ---- discover() Tests ----


class TestDiscover:
    """Tests for ClaudeIngestor.discover()."""

    def test_finds_claude_export_json(
        self, ingestor: ClaudeIngestor, sample_export_dir: Path
    ) -> None:
        """discover() should find JSON files that match Claude export format."""
        docs = ingestor.discover(sample_export_dir)
        assert len(docs) >= 1
        assert all(isinstance(d, RawDocument) for d in docs)

    def test_returns_raw_documents(
        self, ingestor: ClaudeIngestor, sample_export_dir: Path
    ) -> None:
        """discover() should return RawDocument instances."""
        docs = ingestor.discover(sample_export_dir)
        assert len(docs) > 0
        doc = docs[0]
        assert isinstance(doc.path, Path)
        assert isinstance(doc.content, bytes)
        assert doc.detected_encoding == "utf-8"

    def test_ignores_non_claude_json(
        self, ingestor: ClaudeIngestor, tmp_path: Path
    ) -> None:
        """discover() should skip JSON files that are not Claude exports."""
        non_claude = tmp_path / "other.json"
        non_claude.write_text('{"type": "not_claude"}')
        docs = ingestor.discover(tmp_path)
        assert len(docs) == 0

    def test_empty_directory(self, ingestor: ClaudeIngestor, tmp_path: Path) -> None:
        """discover() should return empty list for a directory with no JSON."""
        docs = ingestor.discover(tmp_path)
        assert docs == []

    def test_ignores_non_json_files(
        self, ingestor: ClaudeIngestor, tmp_path: Path
    ) -> None:
        """discover() should skip files that are not .json."""
        txt_file = tmp_path / "notes.txt"
        txt_file.write_text("not json")
        docs = ingestor.discover(tmp_path)
        assert docs == []

    def test_handles_invalid_json_gracefully(
        self, ingestor: ClaudeIngestor, tmp_path: Path
    ) -> None:
        """discover() should skip files containing invalid JSON."""
        bad_json = tmp_path / "broken.json"
        bad_json.write_text("{not valid json")
        docs = ingestor.discover(tmp_path)
        assert docs == []

    def test_discovers_multiple_files(
        self, ingestor: ClaudeIngestor, tmp_path: Path
    ) -> None:
        """discover() should find multiple Claude export files."""
        for i in range(3):
            conv = _make_single_conversation(uuid=f"conv-{i}")
            data = _make_claude_export([conv])
            (tmp_path / f"export_{i}.json").write_bytes(data)
        docs = ingestor.discover(tmp_path)
        assert len(docs) == 3

    def test_single_conversation_format(
        self, ingestor: ClaudeIngestor, tmp_path: Path
    ) -> None:
        """discover() should handle single-conversation format (no wrapper)."""
        single_conv = {
            "conversation_id": "conv-single",
            "title": "Single conversation",
            "create_time": "2024-11-15T10:30:00Z",
            "messages": [
                {
                    "role": "human",
                    "content": "Hello",
                    "timestamp": "2024-11-15T10:30:00Z",
                },
            ],
        }
        export_file = tmp_path / "single.json"
        export_file.write_text(json.dumps(single_conv))
        docs = ingestor.discover(tmp_path)
        assert len(docs) == 1


# ---- parse() Tests ----


class TestParse:
    """Tests for ClaudeIngestor.parse()."""

    def test_extracts_fragments_from_conversation(
        self, ingestor: ClaudeIngestor, sample_raw_doc: RawDocument
    ) -> None:
        """parse() should extract fragments from conversation turns."""
        fragments = ingestor.parse(sample_raw_doc)
        assert len(fragments) >= 1
        assert all(isinstance(f, ParsedFragment) for f in fragments)

    def test_human_assistant_pair_becomes_fragment(
        self, ingestor: ClaudeIngestor
    ) -> None:
        """Each human+assistant turn pair should produce one fragment."""
        conv = _make_single_conversation(
            messages=[
                {
                    "role": "human",
                    "content": "Question 1?",
                    "created_at": "2024-11-15T10:00:00Z",
                },
                {
                    "role": "assistant",
                    "content": "Answer 1.",
                    "created_at": "2024-11-15T10:00:15Z",
                },
                {
                    "role": "human",
                    "content": "Question 2?",
                    "created_at": "2024-11-15T10:01:00Z",
                },
                {
                    "role": "assistant",
                    "content": "Answer 2.",
                    "created_at": "2024-11-15T10:01:15Z",
                },
            ],
        )
        raw = _raw_doc_from_export(_make_claude_export([conv]))
        fragments = ingestor.parse(raw)
        assert len(fragments) == 2

    def test_preserves_conversation_metadata(self, ingestor: ClaudeIngestor) -> None:
        """parse() should preserve conversation_id and model in metadata."""
        conv = _make_single_conversation(
            uuid="conv-meta-test",
            model="claude-3-opus-20240229",
        )
        raw = _raw_doc_from_export(_make_claude_export([conv]))
        fragments = ingestor.parse(raw)
        assert len(fragments) >= 1
        meta = fragments[0].metadata
        assert meta["conversation_id"] == "conv-meta-test"
        assert meta["model"] == "claude-3-opus-20240229"

    def test_preserves_conversation_name(self, ingestor: ClaudeIngestor) -> None:
        """parse() should preserve conversation name in metadata."""
        conv = _make_single_conversation(name="Important Discussion")
        raw = _raw_doc_from_export(_make_claude_export([conv]))
        fragments = ingestor.parse(raw)
        assert fragments[0].metadata["conversation_name"] == "Important Discussion"

    def test_fragment_content_contains_both_turns(
        self, ingestor: ClaudeIngestor
    ) -> None:
        """Fragment content should include both human and assistant text."""
        conv = _make_single_conversation(
            messages=[
                {
                    "role": "human",
                    "content": "My specific question",
                    "created_at": "2024-11-15T10:00:00Z",
                },
                {
                    "role": "assistant",
                    "content": "The specific answer",
                    "created_at": "2024-11-15T10:00:15Z",
                },
            ],
        )
        raw = _raw_doc_from_export(_make_claude_export([conv]))
        fragments = ingestor.parse(raw)
        assert "My specific question" in fragments[0].content
        assert "The specific answer" in fragments[0].content

    def test_timestamp_from_human_turn(self, ingestor: ClaudeIngestor) -> None:
        """Fragment timestamp should come from the human turn."""
        conv = _make_single_conversation(
            messages=[
                {
                    "role": "human",
                    "content": "Hello",
                    "created_at": "2024-11-15T10:30:00Z",
                },
                {
                    "role": "assistant",
                    "content": "Hi there",
                    "created_at": "2024-11-15T10:30:15Z",
                },
            ],
        )
        raw = _raw_doc_from_export(_make_claude_export([conv]))
        fragments = ingestor.parse(raw)
        ts = fragments[0].timestamp
        assert ts.tzinfo is not None
        # Should be normalized to LA timezone
        assert str(ts.tzinfo) == "America/Los_Angeles"

    def test_source_path_set(
        self, ingestor: ClaudeIngestor, sample_raw_doc: RawDocument
    ) -> None:
        """Fragment source_path should match the raw document path."""
        fragments = ingestor.parse(sample_raw_doc)
        assert len(fragments) > 0
        assert fragments[0].source_path == str(sample_raw_doc.path)

    def test_multiple_conversations_in_export(self, ingestor: ClaudeIngestor) -> None:
        """parse() should handle exports with multiple conversations."""
        conv1 = _make_single_conversation(uuid="conv-1", name="First")
        conv2 = _make_single_conversation(uuid="conv-2", name="Second")
        raw = _raw_doc_from_export(_make_claude_export([conv1, conv2]))
        fragments = ingestor.parse(raw)
        assert len(fragments) == 2
        ids = {f.metadata["conversation_id"] for f in fragments}
        assert ids == {"conv-1", "conv-2"}

    def test_empty_conversations_list(self, ingestor: ClaudeIngestor) -> None:
        """parse() should return empty list for export with no conversations."""
        raw = _raw_doc_from_export(_make_claude_export([]))
        fragments = ingestor.parse(raw)
        assert fragments == []

    def test_conversation_with_no_messages(self, ingestor: ClaudeIngestor) -> None:
        """parse() should skip conversations with empty message lists."""
        conv = _make_single_conversation(messages=[])
        raw = _raw_doc_from_export(_make_claude_export([conv]))
        fragments = ingestor.parse(raw)
        assert fragments == []

    def test_skips_system_prompts(self, ingestor: ClaudeIngestor) -> None:
        """parse() should skip system prompt messages."""
        conv = _make_single_conversation(
            messages=[
                {
                    "role": "system",
                    "content": "You are a helpful assistant.",
                    "created_at": "2024-11-15T10:00:00Z",
                },
                {
                    "role": "human",
                    "content": "Hello",
                    "created_at": "2024-11-15T10:00:01Z",
                },
                {
                    "role": "assistant",
                    "content": "Hi!",
                    "created_at": "2024-11-15T10:00:02Z",
                },
            ],
        )
        raw = _raw_doc_from_export(_make_claude_export([conv]))
        fragments = ingestor.parse(raw)
        assert len(fragments) == 1
        assert "You are a helpful assistant" not in fragments[0].content

    def test_handles_missing_model_field(self, ingestor: ClaudeIngestor) -> None:
        """parse() should handle conversations without a model field."""
        conv = _make_single_conversation(model="")
        # model="" is falsy, so the helper won't add the key at all
        assert "model" not in conv
        raw = _raw_doc_from_export(_make_claude_export([conv]))
        fragments = ingestor.parse(raw)
        assert len(fragments) >= 1
        assert "model" not in fragments[0].metadata

    def test_handles_missing_created_at_on_message(
        self, ingestor: ClaudeIngestor
    ) -> None:
        """parse() should handle messages missing a created_at timestamp."""
        conv = _make_single_conversation(
            messages=[
                {
                    "role": "human",
                    "content": "No timestamp",
                },
                {
                    "role": "assistant",
                    "content": "Also no timestamp",
                },
            ],
        )
        raw = _raw_doc_from_export(_make_claude_export([conv]))
        fragments = ingestor.parse(raw)
        # Should still produce a fragment, using conversation-level timestamp
        assert len(fragments) >= 1

    def test_trailing_human_without_response(self, ingestor: ClaudeIngestor) -> None:
        """parse() should handle a trailing human turn with no assistant reply."""
        conv = _make_single_conversation(
            messages=[
                {
                    "role": "human",
                    "content": "First question",
                    "created_at": "2024-11-15T10:00:00Z",
                },
                {
                    "role": "assistant",
                    "content": "First answer",
                    "created_at": "2024-11-15T10:00:15Z",
                },
                {
                    "role": "human",
                    "content": "Unanswered question",
                    "created_at": "2024-11-15T10:01:00Z",
                },
            ],
        )
        raw = _raw_doc_from_export(_make_claude_export([conv]))
        fragments = ingestor.parse(raw)
        # The trailing human turn should not produce a fragment
        assert len(fragments) == 1

    def test_single_conversation_format_parse(self, ingestor: ClaudeIngestor) -> None:
        """parse() should handle the single-conversation format."""
        single_conv = {
            "conversation_id": "conv-single",
            "title": "Single conv",
            "create_time": "2024-11-15T10:30:00Z",
            "messages": [
                {
                    "role": "human",
                    "content": "Hello",
                    "timestamp": "2024-11-15T10:30:00Z",
                },
                {
                    "role": "assistant",
                    "content": "Hi there!",
                    "timestamp": "2024-11-15T10:30:10Z",
                },
            ],
        }
        raw = _raw_doc_from_export(json.dumps(single_conv).encode("utf-8"))
        fragments = ingestor.parse(raw)
        assert len(fragments) == 1
        assert fragments[0].metadata["conversation_id"] == "conv-single"

    def test_handles_content_as_list(self, ingestor: ClaudeIngestor) -> None:
        """parse() should handle content structured as a list of parts."""
        conv = _make_single_conversation(
            messages=[
                {
                    "role": "human",
                    "content": [{"type": "text", "text": "Hello from list"}],
                    "created_at": "2024-11-15T10:00:00Z",
                },
                {
                    "role": "assistant",
                    "content": "Normal response",
                    "created_at": "2024-11-15T10:00:15Z",
                },
            ],
        )
        raw = _raw_doc_from_export(_make_claude_export([conv]))
        fragments = ingestor.parse(raw)
        assert len(fragments) == 1
        assert "Hello from list" in fragments[0].content

    def test_turn_index_in_metadata(self, ingestor: ClaudeIngestor) -> None:
        """parse() should include the turn index in fragment metadata."""
        conv = _make_single_conversation(
            messages=[
                {
                    "role": "human",
                    "content": "Q1",
                    "created_at": "2024-11-15T10:00:00Z",
                },
                {
                    "role": "assistant",
                    "content": "A1",
                    "created_at": "2024-11-15T10:00:15Z",
                },
                {
                    "role": "human",
                    "content": "Q2",
                    "created_at": "2024-11-15T10:01:00Z",
                },
                {
                    "role": "assistant",
                    "content": "A2",
                    "created_at": "2024-11-15T10:01:15Z",
                },
            ],
        )
        raw = _raw_doc_from_export(_make_claude_export([conv]))
        fragments = ingestor.parse(raw)
        assert fragments[0].metadata["turn_index"] == 0
        assert fragments[1].metadata["turn_index"] == 1


# ---- convert_to_markdown() Tests ----


class TestConvertToMarkdown:
    """Tests for ClaudeIngestor.convert_to_markdown()."""

    def test_returns_string(
        self, ingestor: ClaudeIngestor, sample_raw_doc: RawDocument
    ) -> None:
        """convert_to_markdown() should return a string."""
        fragments = ingestor.parse(sample_raw_doc)
        markdown = ingestor.convert_to_markdown(fragments[0])
        assert isinstance(markdown, str)

    def test_human_turn_as_blockquote(self, ingestor: ClaudeIngestor) -> None:
        """Human turn should be formatted as a blockquote."""
        conv = _make_single_conversation(
            messages=[
                {
                    "role": "human",
                    "content": "My question here",
                    "created_at": "2024-11-15T10:00:00Z",
                },
                {
                    "role": "assistant",
                    "content": "My answer here",
                    "created_at": "2024-11-15T10:00:15Z",
                },
            ],
        )
        raw = _raw_doc_from_export(_make_claude_export([conv]))
        fragments = ingestor.parse(raw)
        markdown = ingestor.convert_to_markdown(fragments[0])
        assert "> My question here" in markdown

    def test_assistant_response_not_blockquoted(self, ingestor: ClaudeIngestor) -> None:
        """Assistant response should appear as plain text (not blockquoted)."""
        conv = _make_single_conversation(
            messages=[
                {
                    "role": "human",
                    "content": "Question",
                    "created_at": "2024-11-15T10:00:00Z",
                },
                {
                    "role": "assistant",
                    "content": "The answer text",
                    "created_at": "2024-11-15T10:00:15Z",
                },
            ],
        )
        raw = _raw_doc_from_export(_make_claude_export([conv]))
        fragments = ingestor.parse(raw)
        markdown = ingestor.convert_to_markdown(fragments[0])
        assert "The answer text" in markdown
        # The answer should not itself be blockquoted
        lines = markdown.split("\n")
        answer_lines = [line for line in lines if "The answer text" in line]
        assert len(answer_lines) > 0
        assert not answer_lines[0].startswith(">")

    def test_markdown_contains_both_turns(self, ingestor: ClaudeIngestor) -> None:
        """Markdown output should contain both the human and assistant text."""
        conv = _make_single_conversation(
            messages=[
                {
                    "role": "human",
                    "content": "HUMAN_CONTENT_ABC",
                    "created_at": "2024-11-15T10:00:00Z",
                },
                {
                    "role": "assistant",
                    "content": "ASSISTANT_CONTENT_XYZ",
                    "created_at": "2024-11-15T10:00:15Z",
                },
            ],
        )
        raw = _raw_doc_from_export(_make_claude_export([conv]))
        fragments = ingestor.parse(raw)
        markdown = ingestor.convert_to_markdown(fragments[0])
        assert "HUMAN_CONTENT_ABC" in markdown
        assert "ASSISTANT_CONTENT_XYZ" in markdown

    def test_multiline_human_content_blockquoted(
        self, ingestor: ClaudeIngestor
    ) -> None:
        """Multi-line human content should have each line blockquoted."""
        conv = _make_single_conversation(
            messages=[
                {
                    "role": "human",
                    "content": "Line one\nLine two\nLine three",
                    "created_at": "2024-11-15T10:00:00Z",
                },
                {
                    "role": "assistant",
                    "content": "Response",
                    "created_at": "2024-11-15T10:00:15Z",
                },
            ],
        )
        raw = _raw_doc_from_export(_make_claude_export([conv]))
        fragments = ingestor.parse(raw)
        markdown = ingestor.convert_to_markdown(fragments[0])
        assert "> Line one" in markdown
        assert "> Line two" in markdown
        assert "> Line three" in markdown


# ---- generate_frontmatter() Tests ----


class TestGenerateFrontmatter:
    """Tests for ClaudeIngestor.generate_frontmatter()."""

    def test_returns_dict(
        self, ingestor: ClaudeIngestor, sample_raw_doc: RawDocument
    ) -> None:
        """generate_frontmatter() should return a dict."""
        fragments = ingestor.parse(sample_raw_doc)
        fm = ingestor.generate_frontmatter(fragments[0])
        assert isinstance(fm, dict)

    def test_source_platform_is_claude(
        self, ingestor: ClaudeIngestor, sample_raw_doc: RawDocument
    ) -> None:
        """Frontmatter should have source.platform set to 'claude'."""
        fragments = ingestor.parse(sample_raw_doc)
        fm = ingestor.generate_frontmatter(fragments[0])
        assert fm["source"]["platform"] == "claude"

    def test_source_conversation_id(self, ingestor: ClaudeIngestor) -> None:
        """Frontmatter should include the conversation_id."""
        conv = _make_single_conversation(uuid="conv-fm-test")
        raw = _raw_doc_from_export(_make_claude_export([conv]))
        fragments = ingestor.parse(raw)
        fm = ingestor.generate_frontmatter(fragments[0])
        assert fm["source"]["conversation_id"] == "conv-fm-test"

    def test_timestamp_in_la_timezone(self, ingestor: ClaudeIngestor) -> None:
        """Frontmatter created timestamp should be in LA timezone."""
        conv = _make_single_conversation(
            messages=[
                {
                    "role": "human",
                    "content": "Test",
                    "created_at": "2024-11-15T18:30:00Z",
                },
                {
                    "role": "assistant",
                    "content": "Reply",
                    "created_at": "2024-11-15T18:30:15Z",
                },
            ],
        )
        raw = _raw_doc_from_export(_make_claude_export([conv]))
        fragments = ingestor.parse(raw)
        fm = ingestor.generate_frontmatter(fragments[0])
        created_str = fm["created"]
        assert isinstance(created_str, str)
        # The fragment timestamp itself should be in LA timezone
        ts = fragments[0].timestamp
        assert str(ts.tzinfo) == "America/Los_Angeles"
        # 18:30 UTC in November = 10:30 PST (UTC-8)
        assert ts.hour == 10
        assert ts.minute == 30

    def test_includes_conversation_name(self, ingestor: ClaudeIngestor) -> None:
        """Frontmatter should include the conversation name as title."""
        conv = _make_single_conversation(name="My Great Chat")
        raw = _raw_doc_from_export(_make_claude_export([conv]))
        fragments = ingestor.parse(raw)
        fm = ingestor.generate_frontmatter(fragments[0])
        assert "My Great Chat" in fm.get("title", "")

    def test_includes_model_info(self, ingestor: ClaudeIngestor) -> None:
        """Frontmatter should include model information when available."""
        conv = _make_single_conversation(model="claude-3-sonnet-20240229")
        raw = _raw_doc_from_export(_make_claude_export([conv]))
        fragments = ingestor.parse(raw)
        fm = ingestor.generate_frontmatter(fragments[0])
        assert fm["source"].get("model") == "claude-3-sonnet-20240229"

    def test_frontmatter_without_model(self, ingestor: ClaudeIngestor) -> None:
        """Frontmatter should still work when model is not present."""
        conv = _make_single_conversation(model="")
        # model="" is falsy, so the key is not in conv
        assert "model" not in conv
        raw = _raw_doc_from_export(_make_claude_export([conv]))
        fragments = ingestor.parse(raw)
        fm = ingestor.generate_frontmatter(fragments[0])
        assert fm["source"]["platform"] == "claude"


# ---- ingest() Integration Tests ----


class TestIngestIntegration:
    """Tests for the full ingest() pipeline with ClaudeIngestor."""

    def test_full_pipeline(
        self, ingestor: ClaudeIngestor, sample_export_dir: Path
    ) -> None:
        """Full ingest pipeline should produce fragments with provenance."""
        result = ingestor.ingest(sample_export_dir)
        assert isinstance(result, IngestResult)
        assert len(result.fragments) >= 1
        assert len(result.provenance) >= 1
        assert result.errors == []

    def test_fragments_have_markdown(
        self, ingestor: ClaudeIngestor, sample_export_dir: Path
    ) -> None:
        """Ingested fragments should have markdown in metadata."""
        result = ingestor.ingest(sample_export_dir)
        assert len(result.fragments) > 0
        frag = result.fragments[0]
        assert "markdown" in frag.metadata
        assert isinstance(frag.metadata["markdown"], str)
        assert len(frag.metadata["markdown"]) > 0

    def test_fragments_have_frontmatter(
        self, ingestor: ClaudeIngestor, sample_export_dir: Path
    ) -> None:
        """Ingested fragments should have frontmatter in metadata."""
        result = ingestor.ingest(sample_export_dir)
        assert len(result.fragments) > 0
        frag = result.fragments[0]
        assert "frontmatter" in frag.metadata
        assert isinstance(frag.metadata["frontmatter"], dict)

    def test_provenance_records_success(
        self, ingestor: ClaudeIngestor, sample_export_dir: Path
    ) -> None:
        """Provenance entries should record success status."""
        result = ingestor.ingest(sample_export_dir)
        assert len(result.provenance) > 0
        assert all(p.status == "success" for p in result.provenance)

    def test_empty_directory_produces_no_fragments(
        self, ingestor: ClaudeIngestor, tmp_path: Path
    ) -> None:
        """Ingesting an empty directory should produce no fragments."""
        result = ingestor.ingest(tmp_path)
        assert result.fragments == []
        assert result.errors == []


# ---- Edge Case Tests ----


class TestEdgeCases:
    """Tests for edge cases and error handling."""

    def test_empty_message_content(self, ingestor: ClaudeIngestor) -> None:
        """parse() should handle messages with empty content."""
        conv = _make_single_conversation(
            messages=[
                {
                    "role": "human",
                    "content": "",
                    "created_at": "2024-11-15T10:00:00Z",
                },
                {
                    "role": "assistant",
                    "content": "",
                    "created_at": "2024-11-15T10:00:15Z",
                },
            ],
        )
        raw = _raw_doc_from_export(_make_claude_export([conv]))
        fragments = ingestor.parse(raw)
        # Empty exchanges should still produce a fragment (or be skipped)
        # Either behavior is acceptable
        assert isinstance(fragments, list)

    def test_very_long_content(self, ingestor: ClaudeIngestor) -> None:
        """parse() should handle very long message content."""
        long_text = "word " * 10000
        conv = _make_single_conversation(
            messages=[
                {
                    "role": "human",
                    "content": long_text,
                    "created_at": "2024-11-15T10:00:00Z",
                },
                {
                    "role": "assistant",
                    "content": long_text,
                    "created_at": "2024-11-15T10:00:15Z",
                },
            ],
        )
        raw = _raw_doc_from_export(_make_claude_export([conv]))
        fragments = ingestor.parse(raw)
        assert len(fragments) == 1
        assert "word" in fragments[0].content

    def test_unicode_content(self, ingestor: ClaudeIngestor) -> None:
        """parse() should handle Unicode content correctly."""
        conv = _make_single_conversation(
            messages=[
                {
                    "role": "human",
                    "content": "Bonjour, comment allez-vous? \u2603\u2764\ufe0f",
                    "created_at": "2024-11-15T10:00:00Z",
                },
                {
                    "role": "assistant",
                    "content": "Je vais bien, merci! \u2728",
                    "created_at": "2024-11-15T10:00:15Z",
                },
            ],
        )
        raw = _raw_doc_from_export(_make_claude_export([conv]))
        fragments = ingestor.parse(raw)
        assert len(fragments) == 1
        assert "\u2603" in fragments[0].content

    def test_consecutive_human_messages(self, ingestor: ClaudeIngestor) -> None:
        """parse() should handle consecutive human messages correctly."""
        conv = _make_single_conversation(
            messages=[
                {
                    "role": "human",
                    "content": "First human message",
                    "created_at": "2024-11-15T10:00:00Z",
                },
                {
                    "role": "human",
                    "content": "Second human message",
                    "created_at": "2024-11-15T10:00:30Z",
                },
                {
                    "role": "assistant",
                    "content": "Response to both",
                    "created_at": "2024-11-15T10:00:45Z",
                },
            ],
        )
        raw = _raw_doc_from_export(_make_claude_export([conv]))
        fragments = ingestor.parse(raw)
        # Should handle gracefully; at least one fragment produced
        assert len(fragments) >= 1

    def test_conversation_missing_uuid(self, ingestor: ClaudeIngestor) -> None:
        """parse() should handle conversations missing uuid."""
        conv = _make_single_conversation()
        del conv["uuid"]
        raw = _raw_doc_from_export(_make_claude_export([conv]))
        fragments = ingestor.parse(raw)
        assert len(fragments) >= 1
        # Should generate or use a fallback conversation_id
        assert "conversation_id" in fragments[0].metadata

    def test_conversation_missing_name(self, ingestor: ClaudeIngestor) -> None:
        """parse() should handle conversations missing a name."""
        conv = _make_single_conversation()
        del conv["name"]
        raw = _raw_doc_from_export(_make_claude_export([conv]))
        fragments = ingestor.parse(raw)
        assert len(fragments) >= 1
        assert "conversation_name" in fragments[0].metadata

    def test_existing_fixture_file(self, ingestor: ClaudeIngestor) -> None:
        """parse() should handle the existing sample_claude_export.json fixture."""
        fixture_path = Path(__file__).parent / "fixtures" / "sample_claude_export.json"
        content = fixture_path.read_bytes()
        raw = RawDocument(
            path=fixture_path,
            content=content,
            metadata={},
            detected_encoding="utf-8",
        )
        fragments = ingestor.parse(raw)
        assert len(fragments) >= 1
        # The fixture has a human turn + assistant response
        assert "knowledge" in fragments[0].content.lower()

    def test_json_data_without_conversations_or_messages(
        self, ingestor: ClaudeIngestor
    ) -> None:
        """parse() should return empty list for JSON with no conversation keys."""
        data = {"some_key": "some_value"}
        raw = _raw_doc_from_export(json.dumps(data).encode("utf-8"))
        fragments = ingestor.parse(raw)
        assert fragments == []

    def test_discover_skips_json_array(
        self, ingestor: ClaudeIngestor, tmp_path: Path
    ) -> None:
        """discover() should skip JSON files whose root is an array, not a dict."""
        array_file = tmp_path / "array.json"
        array_file.write_text('[{"msg": "hello"}]')
        docs = ingestor.discover(tmp_path)
        assert docs == []

    def test_content_as_list_with_non_text_parts(
        self, ingestor: ClaudeIngestor
    ) -> None:
        """parse() should handle list content where some parts are not text."""
        conv = _make_single_conversation(
            messages=[
                {
                    "role": "human",
                    "content": [
                        {"type": "image", "url": "http://example.com/img.png"},
                        {"type": "text", "text": "Describe this image"},
                    ],
                    "created_at": "2024-11-15T10:00:00Z",
                },
                {
                    "role": "assistant",
                    "content": "It shows a landscape.",
                    "created_at": "2024-11-15T10:00:15Z",
                },
            ],
        )
        raw = _raw_doc_from_export(_make_claude_export([conv]))
        fragments = ingestor.parse(raw)
        assert len(fragments) == 1
        assert "Describe this image" in fragments[0].content
        # Non-text parts should be excluded
        assert "http://example.com" not in fragments[0].content
