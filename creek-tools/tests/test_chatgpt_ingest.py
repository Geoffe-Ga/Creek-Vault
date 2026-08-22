"""Tests for creek.ingest.chatgpt — ChatGPT conversation JSON ingestor.

Covers discovery of ChatGPT JSON export files, parsing of tree-structured
conversation mappings, conversion to markdown with blockquote format,
frontmatter generation with chatgpt platform metadata, branching conversation
handling, and edge cases (missing content, null messages, empty trees).
"""

import json
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from creek.ingest.base import (
    Ingestor,
    IngestResult,
    ParsedFragment,
    RawDocument,
)
from creek.ingest.chatgpt import ChatGPTIngestor

# ---- Constants ----

LA_TZ = ZoneInfo("America/Los_Angeles")
FIXTURES = Path(__file__).parent / "fixtures"


# ---- Helpers ----


def _make_raw_doc(data: list[dict[str, Any]], path: str = "conv.json") -> RawDocument:
    """Create a RawDocument from a list of ChatGPT conversation dicts.

    Args:
        data: The conversation list to serialize as JSON.
        path: The file path for the RawDocument.

    Returns:
        A RawDocument containing the JSON-encoded data.
    """
    raw_bytes = json.dumps(data).encode("utf-8")
    return RawDocument(
        path=Path(path),
        content=raw_bytes,
        metadata={},
        detected_encoding="utf-8",
    )


def _minimal_conversation(
    title: str = "Test Chat",
    create_time: float = 1700042400.0,
) -> dict[str, Any]:
    """Build a minimal ChatGPT conversation with one user+assistant pair.

    Args:
        title: Conversation title.
        create_time: Unix epoch timestamp for conversation creation.

    Returns:
        A dict representing one ChatGPT conversation.
    """
    return {
        "title": title,
        "create_time": create_time,
        "update_time": create_time + 100.0,
        "mapping": {
            "root": {
                "id": "root",
                "message": None,
                "parent": None,
                "children": ["sys"],
            },
            "sys": {
                "id": "sys",
                "message": {
                    "id": "sys",
                    "author": {"role": "system"},
                    "content": {"content_type": "text", "parts": ["System prompt."]},
                    "create_time": create_time,
                },
                "parent": "root",
                "children": ["u1"],
            },
            "u1": {
                "id": "u1",
                "message": {
                    "id": "u1",
                    "author": {"role": "user"},
                    "content": {"content_type": "text", "parts": ["Hello!"]},
                    "create_time": create_time + 10.0,
                },
                "parent": "sys",
                "children": ["a1"],
            },
            "a1": {
                "id": "a1",
                "message": {
                    "id": "a1",
                    "author": {"role": "assistant"},
                    "content": {"content_type": "text", "parts": ["Hi there!"]},
                    "create_time": create_time + 20.0,
                },
                "parent": "u1",
                "children": [],
            },
        },
    }


# ---- ChatGPTIngestor Subclass Contract Tests ----


class TestChatGPTIngestorContract:
    """Tests that ChatGPTIngestor satisfies the Ingestor ABC contract."""

    def test_is_ingestor_subclass(self) -> None:
        """ChatGPTIngestor should be a subclass of Ingestor."""
        assert issubclass(ChatGPTIngestor, Ingestor)

    def test_instantiates_without_error(self) -> None:
        """ChatGPTIngestor should be instantiable."""
        ingestor = ChatGPTIngestor()
        assert isinstance(ingestor, Ingestor)

    def test_has_discover_method(self) -> None:
        """ChatGPTIngestor should implement the discover method."""
        ingestor = ChatGPTIngestor()
        assert callable(getattr(ingestor, "discover", None))

    def test_has_parse_method(self) -> None:
        """ChatGPTIngestor should implement the parse method."""
        ingestor = ChatGPTIngestor()
        assert callable(getattr(ingestor, "parse", None))

    def test_has_convert_to_markdown_method(self) -> None:
        """ChatGPTIngestor should implement convert_to_markdown."""
        ingestor = ChatGPTIngestor()
        assert callable(getattr(ingestor, "convert_to_markdown", None))

    def test_has_generate_frontmatter_method(self) -> None:
        """ChatGPTIngestor should implement generate_frontmatter."""
        ingestor = ChatGPTIngestor()
        assert callable(getattr(ingestor, "generate_frontmatter", None))


# ---- discover() Tests ----


class TestChatGPTDiscover:
    """Tests for ChatGPTIngestor.discover()."""

    def test_discovers_conversations_json(self, tmp_path: Path) -> None:
        """discover() should find conversations.json files."""
        conv_file = tmp_path / "conversations.json"
        conv_file.write_text("[]")
        ingestor = ChatGPTIngestor()
        docs = ingestor.discover(tmp_path)
        assert len(docs) == 1
        assert docs[0].path == conv_file

    def test_discovers_json_files(self, tmp_path: Path) -> None:
        """discover() should find .json files in the source directory."""
        json_file = tmp_path / "export.json"
        json_file.write_text("[]")
        ingestor = ChatGPTIngestor()
        docs = ingestor.discover(tmp_path)
        assert len(docs) >= 1

    def test_ignores_non_json_files(self, tmp_path: Path) -> None:
        """discover() should ignore non-JSON files."""
        (tmp_path / "readme.txt").write_text("not json")
        (tmp_path / "data.json").write_text("[]")
        ingestor = ChatGPTIngestor()
        docs = ingestor.discover(tmp_path)
        assert all(str(d.path).endswith(".json") for d in docs)

    def test_returns_empty_for_empty_directory(self, tmp_path: Path) -> None:
        """discover() should return empty list for empty directory."""
        ingestor = ChatGPTIngestor()
        docs = ingestor.discover(tmp_path)
        assert docs == []

    def test_raw_document_has_bytes_content(self, tmp_path: Path) -> None:
        """discover() should return RawDocuments with bytes content."""
        conv_file = tmp_path / "conversations.json"
        conv_file.write_text('[{"title": "test"}]')
        ingestor = ChatGPTIngestor()
        docs = ingestor.discover(tmp_path)
        assert isinstance(docs[0].content, bytes)

    def test_raw_document_has_encoding(self, tmp_path: Path) -> None:
        """discover() should detect encoding for each document."""
        conv_file = tmp_path / "conversations.json"
        conv_file.write_text("[]")
        ingestor = ChatGPTIngestor()
        docs = ingestor.discover(tmp_path)
        assert isinstance(docs[0].detected_encoding, str)

    def test_skips_non_list_json(self, tmp_path: Path) -> None:
        """discover() should skip JSON files that are not a list of dicts."""
        # A Claude-style dict export should be rejected
        (tmp_path / "claude_export.json").write_text('{"conversations": []}')
        ingestor = ChatGPTIngestor()
        docs = ingestor.discover(tmp_path)
        assert docs == []

    def test_skips_json_with_non_dict_items(self, tmp_path: Path) -> None:
        """discover() should skip JSON files whose list contains non-dicts."""
        (tmp_path / "strings.json").write_text('["a", "b", "c"]')
        ingestor = ChatGPTIngestor()
        docs = ingestor.discover(tmp_path)
        assert docs == []

    def test_skips_invalid_json(self, tmp_path: Path) -> None:
        """discover() should skip files with invalid JSON."""
        (tmp_path / "broken.json").write_text("{not valid json")
        ingestor = ChatGPTIngestor()
        docs = ingestor.discover(tmp_path)
        assert docs == []

    def test_accepts_valid_chatgpt_export(self, tmp_path: Path) -> None:
        """discover() should accept a JSON list of dicts."""
        (tmp_path / "valid.json").write_text('[{"title": "test"}]')
        ingestor = ChatGPTIngestor()
        docs = ingestor.discover(tmp_path)
        assert len(docs) == 1


# ---- parse() Tests ----


class TestChatGPTParse:
    """Tests for ChatGPTIngestor.parse()."""

    def test_parses_single_conversation(self) -> None:
        """parse() should extract fragments from a single conversation."""
        conv = _minimal_conversation()
        raw = _make_raw_doc([conv])
        ingestor = ChatGPTIngestor()
        fragments = ingestor.parse(raw)
        # One user+assistant pair → human + AI fragment.
        assert len(fragments) == 2

    def test_parses_multiple_conversations(self) -> None:
        """parse() should handle multiple conversations in one file."""
        conv1 = _minimal_conversation(title="Chat 1", create_time=1700042400.0)
        conv2 = _minimal_conversation(title="Chat 2", create_time=1700128800.0)
        raw = _make_raw_doc([conv1, conv2])
        ingestor = ChatGPTIngestor()
        fragments = ingestor.parse(raw)
        # Two conversations, one pair each → human + AI fragment per pair.
        assert len(fragments) == 4

    def test_fragment_content_has_user_and_assistant(self) -> None:
        """User text lands in the human fragment, assistant in the AI one."""
        conv = _minimal_conversation()
        raw = _make_raw_doc([conv])
        ingestor = ChatGPTIngestor()
        fragments = ingestor.parse(raw)
        # Human fragment (index 0) carries only the user text.
        assert "Hello!" in fragments[0].content
        assert "Hi there!" not in fragments[0].content
        # AI fragment (index 1) carries only the assistant text.
        assert "Hi there!" in fragments[1].content
        assert "Hello!" not in fragments[1].content

    def test_fragment_excludes_system_messages(self) -> None:
        """parse() should skip system messages from fragment content."""
        conv = _minimal_conversation()
        raw = _make_raw_doc([conv])
        ingestor = ChatGPTIngestor()
        fragments = ingestor.parse(raw)
        assert "System prompt." not in fragments[0].content

    def test_fragment_has_timestamp(self) -> None:
        """parse() should set fragment timestamp from create_time."""
        conv = _minimal_conversation(create_time=1700042400.0)
        raw = _make_raw_doc([conv])
        ingestor = ChatGPTIngestor()
        fragments = ingestor.parse(raw)
        assert fragments[0].timestamp.tzinfo is not None

    def test_fragment_timestamp_uses_la_timezone(self) -> None:
        """parse() should normalize timestamp to America/Los_Angeles."""
        conv = _minimal_conversation(create_time=1700042400.0)
        raw = _make_raw_doc([conv])
        ingestor = ChatGPTIngestor()
        fragments = ingestor.parse(raw)
        assert str(fragments[0].timestamp.tzinfo) == "America/Los_Angeles"

    def test_fragment_has_source_path(self) -> None:
        """parse() should set fragment source_path to the file path."""
        conv = _minimal_conversation()
        raw = _make_raw_doc([conv], path="/data/conversations.json")
        ingestor = ChatGPTIngestor()
        fragments = ingestor.parse(raw)
        assert fragments[0].source_path == "/data/conversations.json"

    def test_fragment_metadata_has_title_with_turn_index(self) -> None:
        """parse() should include conversation title with turn index."""
        conv = _minimal_conversation(title="My Chat")
        raw = _make_raw_doc([conv])
        ingestor = ChatGPTIngestor()
        fragments = ingestor.parse(raw)
        assert fragments[0].metadata["title"] == "My Chat (turn 0)"

    def test_fragment_metadata_has_platform(self) -> None:
        """parse() should include platform in fragment metadata."""
        conv = _minimal_conversation()
        raw = _make_raw_doc([conv])
        ingestor = ChatGPTIngestor()
        fragments = ingestor.parse(raw)
        assert fragments[0].metadata["platform"] == "chatgpt"

    def test_multiple_turn_pairs(self) -> None:
        """parse() should create fragments from multiple user+assistant pairs."""
        conv: dict[str, Any] = {
            "title": "Multi-turn",
            "create_time": 1700042400.0,
            "update_time": 1700042500.0,
            "mapping": {
                "root": {
                    "id": "root",
                    "message": None,
                    "parent": None,
                    "children": ["u1"],
                },
                "u1": {
                    "id": "u1",
                    "message": {
                        "id": "u1",
                        "author": {"role": "user"},
                        "content": {
                            "content_type": "text",
                            "parts": ["First question"],
                        },
                        "create_time": 1700042410.0,
                    },
                    "parent": "root",
                    "children": ["a1"],
                },
                "a1": {
                    "id": "a1",
                    "message": {
                        "id": "a1",
                        "author": {"role": "assistant"},
                        "content": {
                            "content_type": "text",
                            "parts": ["First answer"],
                        },
                        "create_time": 1700042420.0,
                    },
                    "parent": "u1",
                    "children": ["u2"],
                },
                "u2": {
                    "id": "u2",
                    "message": {
                        "id": "u2",
                        "author": {"role": "user"},
                        "content": {
                            "content_type": "text",
                            "parts": ["Second question"],
                        },
                        "create_time": 1700042430.0,
                    },
                    "parent": "a1",
                    "children": ["a2"],
                },
                "a2": {
                    "id": "a2",
                    "message": {
                        "id": "a2",
                        "author": {"role": "assistant"},
                        "content": {
                            "content_type": "text",
                            "parts": ["Second answer"],
                        },
                        "create_time": 1700042440.0,
                    },
                    "parent": "u2",
                    "children": [],
                },
            },
        }
        raw = _make_raw_doc([conv])
        ingestor = ChatGPTIngestor()
        fragments = ingestor.parse(raw)
        # Two user+assistant pairs, each split into human + AI = four fragments.
        assert len(fragments) == 4
        assert "First question" in fragments[0].content
        assert "First answer" in fragments[1].content
        assert "Second question" in fragments[2].content
        assert "Second answer" in fragments[3].content

    def test_fixture_file_parses(self) -> None:
        """parse() should handle the sample ChatGPT export fixture."""
        fixture_path = FIXTURES / "sample_chatgpt_export.json"
        raw_bytes = fixture_path.read_bytes()
        raw = RawDocument(
            path=fixture_path,
            content=raw_bytes,
            metadata={},
            detected_encoding="utf-8",
        )
        ingestor = ChatGPTIngestor()
        fragments = ingestor.parse(raw)
        # Fixture has 2 conversations: first 2 pairs, second 1 pair = 3 pairs;
        # each pair splits into a human + AI fragment = six fragments.
        assert len(fragments) == 6


# ---- Branching Conversation Tests ----


class TestChatGPTBranching:
    """Tests for branching conversation handling in ChatGPTIngestor.parse()."""

    def test_follows_longest_branch(self) -> None:
        """parse() should follow the branch with the most messages."""
        conv: dict[str, Any] = {
            "title": "Branching Chat",
            "create_time": 1700042400.0,
            "update_time": 1700042500.0,
            "mapping": {
                "root": {
                    "id": "root",
                    "message": None,
                    "parent": None,
                    "children": ["u1"],
                },
                "u1": {
                    "id": "u1",
                    "message": {
                        "id": "u1",
                        "author": {"role": "user"},
                        "content": {
                            "content_type": "text",
                            "parts": ["Tell me a story"],
                        },
                        "create_time": 1700042410.0,
                    },
                    "parent": "root",
                    "children": ["a1-short", "a1-long"],
                },
                "a1-short": {
                    "id": "a1-short",
                    "message": {
                        "id": "a1-short",
                        "author": {"role": "assistant"},
                        "content": {
                            "content_type": "text",
                            "parts": ["Short branch answer."],
                        },
                        "create_time": 1700042420.0,
                    },
                    "parent": "u1",
                    "children": [],
                },
                "a1-long": {
                    "id": "a1-long",
                    "message": {
                        "id": "a1-long",
                        "author": {"role": "assistant"},
                        "content": {
                            "content_type": "text",
                            "parts": ["Long branch answer."],
                        },
                        "create_time": 1700042425.0,
                    },
                    "parent": "u1",
                    "children": ["u2"],
                },
                "u2": {
                    "id": "u2",
                    "message": {
                        "id": "u2",
                        "author": {"role": "user"},
                        "content": {
                            "content_type": "text",
                            "parts": ["Continue the story"],
                        },
                        "create_time": 1700042430.0,
                    },
                    "parent": "a1-long",
                    "children": ["a2"],
                },
                "a2": {
                    "id": "a2",
                    "message": {
                        "id": "a2",
                        "author": {"role": "assistant"},
                        "content": {
                            "content_type": "text",
                            "parts": ["Story continues here."],
                        },
                        "create_time": 1700042440.0,
                    },
                    "parent": "u2",
                    "children": [],
                },
            },
        }
        raw = _make_raw_doc([conv])
        ingestor = ChatGPTIngestor()
        fragments = ingestor.parse(raw)
        # Follow the long branch (2 pairs → 4 fragments), not the short
        # branch (1 pair → 2 fragments).
        assert len(fragments) == 4
        all_content = " ".join(f.content for f in fragments)
        assert "Long branch answer." in all_content
        assert "Story continues here." in all_content
        # The short branch's answer must not leak in.
        assert "Short branch answer." not in all_content


# ---- Edge Case Tests ----


class TestChatGPTEdgeCases:
    """Tests for edge cases in ChatGPTIngestor.parse()."""

    def test_null_message_in_mapping(self) -> None:
        """parse() should handle null messages in the mapping tree."""
        conv: dict[str, Any] = {
            "title": "Null Message",
            "create_time": 1700042400.0,
            "update_time": 1700042500.0,
            "mapping": {
                "root": {
                    "id": "root",
                    "message": None,
                    "parent": None,
                    "children": ["u1"],
                },
                "u1": {
                    "id": "u1",
                    "message": {
                        "id": "u1",
                        "author": {"role": "user"},
                        "content": {
                            "content_type": "text",
                            "parts": ["Hello"],
                        },
                        "create_time": 1700042410.0,
                    },
                    "parent": "root",
                    "children": ["a1"],
                },
                "a1": {
                    "id": "a1",
                    "message": {
                        "id": "a1",
                        "author": {"role": "assistant"},
                        "content": {
                            "content_type": "text",
                            "parts": ["World"],
                        },
                        "create_time": 1700042420.0,
                    },
                    "parent": "u1",
                    "children": [],
                },
            },
        }
        raw = _make_raw_doc([conv])
        ingestor = ChatGPTIngestor()
        fragments = ingestor.parse(raw)
        # One pair → human + AI fragment.
        assert len(fragments) == 2

    def test_empty_parts_list(self) -> None:
        """parse() should handle messages with empty parts list."""
        conv: dict[str, Any] = {
            "title": "Empty Parts",
            "create_time": 1700042400.0,
            "update_time": 1700042500.0,
            "mapping": {
                "root": {
                    "id": "root",
                    "message": None,
                    "parent": None,
                    "children": ["u1"],
                },
                "u1": {
                    "id": "u1",
                    "message": {
                        "id": "u1",
                        "author": {"role": "user"},
                        "content": {"content_type": "text", "parts": []},
                        "create_time": 1700042410.0,
                    },
                    "parent": "root",
                    "children": ["a1"],
                },
                "a1": {
                    "id": "a1",
                    "message": {
                        "id": "a1",
                        "author": {"role": "assistant"},
                        "content": {"content_type": "text", "parts": []},
                        "create_time": 1700042420.0,
                    },
                    "parent": "u1",
                    "children": [],
                },
            },
        }
        raw = _make_raw_doc([conv])
        ingestor = ChatGPTIngestor()
        fragments = ingestor.parse(raw)
        # Empty content pair should still produce a human + AI fragment.
        assert len(fragments) == 2

    def test_missing_content_key(self) -> None:
        """parse() should handle messages missing the content key."""
        conv: dict[str, Any] = {
            "title": "Missing Content",
            "create_time": 1700042400.0,
            "update_time": 1700042500.0,
            "mapping": {
                "root": {
                    "id": "root",
                    "message": None,
                    "parent": None,
                    "children": ["u1"],
                },
                "u1": {
                    "id": "u1",
                    "message": {
                        "id": "u1",
                        "author": {"role": "user"},
                        "content": {
                            "content_type": "text",
                            "parts": ["Question"],
                        },
                        "create_time": 1700042410.0,
                    },
                    "parent": "root",
                    "children": ["a1"],
                },
                "a1": {
                    "id": "a1",
                    "message": {
                        "id": "a1",
                        "author": {"role": "assistant"},
                        "create_time": 1700042420.0,
                    },
                    "parent": "u1",
                    "children": [],
                },
            },
        }
        raw = _make_raw_doc([conv])
        ingestor = ChatGPTIngestor()
        fragments = ingestor.parse(raw)
        # One pair → human + AI fragment.
        assert len(fragments) == 2

    def test_empty_conversations_list(self) -> None:
        """parse() should return empty list for empty conversations array."""
        raw = _make_raw_doc([])
        ingestor = ChatGPTIngestor()
        fragments = ingestor.parse(raw)
        assert fragments == []

    def test_conversation_with_no_mapping(self) -> None:
        """parse() should skip conversations without mapping key."""
        conv: dict[str, Any] = {
            "title": "No Mapping",
            "create_time": 1700042400.0,
        }
        raw = _make_raw_doc([conv])
        ingestor = ChatGPTIngestor()
        fragments = ingestor.parse(raw)
        assert fragments == []

    def test_multipart_content(self) -> None:
        """parse() should join multiple parts in a message."""
        conv: dict[str, Any] = {
            "title": "Multipart",
            "create_time": 1700042400.0,
            "update_time": 1700042500.0,
            "mapping": {
                "root": {
                    "id": "root",
                    "message": None,
                    "parent": None,
                    "children": ["u1"],
                },
                "u1": {
                    "id": "u1",
                    "message": {
                        "id": "u1",
                        "author": {"role": "user"},
                        "content": {
                            "content_type": "text",
                            "parts": ["Part one.", "Part two."],
                        },
                        "create_time": 1700042410.0,
                    },
                    "parent": "root",
                    "children": ["a1"],
                },
                "a1": {
                    "id": "a1",
                    "message": {
                        "id": "a1",
                        "author": {"role": "assistant"},
                        "content": {
                            "content_type": "text",
                            "parts": ["Response."],
                        },
                        "create_time": 1700042420.0,
                    },
                    "parent": "u1",
                    "children": [],
                },
            },
        }
        raw = _make_raw_doc([conv])
        ingestor = ChatGPTIngestor()
        fragments = ingestor.parse(raw)
        assert "Part one." in fragments[0].content
        assert "Part two." in fragments[0].content

    def test_none_in_parts_list(self) -> None:
        """parse() should handle None values in the parts list."""
        conv: dict[str, Any] = {
            "title": "None Parts",
            "create_time": 1700042400.0,
            "update_time": 1700042500.0,
            "mapping": {
                "root": {
                    "id": "root",
                    "message": None,
                    "parent": None,
                    "children": ["u1"],
                },
                "u1": {
                    "id": "u1",
                    "message": {
                        "id": "u1",
                        "author": {"role": "user"},
                        "content": {
                            "content_type": "text",
                            "parts": [None, "Actual text"],
                        },
                        "create_time": 1700042410.0,
                    },
                    "parent": "root",
                    "children": ["a1"],
                },
                "a1": {
                    "id": "a1",
                    "message": {
                        "id": "a1",
                        "author": {"role": "assistant"},
                        "content": {
                            "content_type": "text",
                            "parts": ["Reply"],
                        },
                        "create_time": 1700042420.0,
                    },
                    "parent": "u1",
                    "children": [],
                },
            },
        }
        raw = _make_raw_doc([conv])
        ingestor = ChatGPTIngestor()
        fragments = ingestor.parse(raw)
        assert "Actual text" in fragments[0].content

    def test_user_without_following_assistant(self) -> None:
        """parse() should skip unpaired user messages at the end."""
        conv: dict[str, Any] = {
            "title": "Unpaired User",
            "create_time": 1700042400.0,
            "update_time": 1700042500.0,
            "mapping": {
                "root": {
                    "id": "root",
                    "message": None,
                    "parent": None,
                    "children": ["u1"],
                },
                "u1": {
                    "id": "u1",
                    "message": {
                        "id": "u1",
                        "author": {"role": "user"},
                        "content": {
                            "content_type": "text",
                            "parts": ["Hello?"],
                        },
                        "create_time": 1700042410.0,
                    },
                    "parent": "root",
                    "children": [],
                },
            },
        }
        raw = _make_raw_doc([conv])
        ingestor = ChatGPTIngestor()
        fragments = ingestor.parse(raw)
        # No assistant response, so no paired fragment
        assert fragments == []

    def test_empty_mapping_dict(self) -> None:
        """parse() should handle an empty mapping dict."""
        conv: dict[str, Any] = {
            "title": "Empty Mapping",
            "create_time": 1700042400.0,
            "mapping": {},
        }
        raw = _make_raw_doc([conv])
        ingestor = ChatGPTIngestor()
        fragments = ingestor.parse(raw)
        assert fragments == []

    def test_tool_role_message_skipped(self) -> None:
        """parse() should skip messages with non-user/assistant/system roles."""
        conv: dict[str, Any] = {
            "title": "Tool Message",
            "create_time": 1700042400.0,
            "update_time": 1700042500.0,
            "mapping": {
                "root": {
                    "id": "root",
                    "message": None,
                    "parent": None,
                    "children": ["tool1"],
                },
                "tool1": {
                    "id": "tool1",
                    "message": {
                        "id": "tool1",
                        "author": {"role": "tool"},
                        "content": {
                            "content_type": "text",
                            "parts": ["Tool output"],
                        },
                        "create_time": 1700042410.0,
                    },
                    "parent": "root",
                    "children": [],
                },
            },
        }
        raw = _make_raw_doc([conv])
        ingestor = ChatGPTIngestor()
        fragments = ingestor.parse(raw)
        assert fragments == []

    def test_missing_create_time_uses_sentinel(self) -> None:
        """parse() should use a fixed sentinel for missing create_time."""
        conv: dict[str, Any] = {
            "title": "No Create Time",
            "mapping": {
                "root": {
                    "id": "root",
                    "message": None,
                    "parent": None,
                    "children": ["u1"],
                },
                "u1": {
                    "id": "u1",
                    "message": {
                        "id": "u1",
                        "author": {"role": "user"},
                        "content": {
                            "content_type": "text",
                            "parts": ["Hello"],
                        },
                    },
                    "parent": "root",
                    "children": ["a1"],
                },
                "a1": {
                    "id": "a1",
                    "message": {
                        "id": "a1",
                        "author": {"role": "assistant"},
                        "content": {
                            "content_type": "text",
                            "parts": ["Hi"],
                        },
                    },
                    "parent": "u1",
                    "children": [],
                },
            },
        }
        raw = _make_raw_doc([conv])
        ingestor = ChatGPTIngestor()
        fragments = ingestor.parse(raw)
        # One pair → human + AI fragment.
        assert len(fragments) == 2
        # Should use the fixed sentinel: 2000-01-01 in LA timezone
        assert fragments[0].timestamp == datetime(2000, 1, 1, tzinfo=LA_TZ)

    def test_zero_epoch_uses_sentinel(self) -> None:
        """parse() should use a fixed sentinel when create_time is 0.

        When the conversation create_time is 0 and message-level
        timestamps are also missing, the sentinel should be used.
        """
        conv: dict[str, Any] = {
            "title": "Zero Epoch",
            "create_time": 0.0,
            "mapping": {
                "root": {
                    "id": "root",
                    "message": None,
                    "parent": None,
                    "children": ["u1"],
                },
                "u1": {
                    "id": "u1",
                    "message": {
                        "id": "u1",
                        "author": {"role": "user"},
                        "content": {"content_type": "text", "parts": ["Hi"]},
                    },
                    "parent": "root",
                    "children": ["a1"],
                },
                "a1": {
                    "id": "a1",
                    "message": {
                        "id": "a1",
                        "author": {"role": "assistant"},
                        "content": {"content_type": "text", "parts": ["Hey"]},
                    },
                    "parent": "u1",
                    "children": [],
                },
            },
        }
        raw = _make_raw_doc([conv])
        ingestor = ChatGPTIngestor()
        fragments = ingestor.parse(raw)
        # One pair → human + AI fragment.
        assert len(fragments) == 2
        # Conversation create_time=0 -> sentinel; no message create_time -> fallback
        assert fragments[0].timestamp == datetime(2000, 1, 1, tzinfo=LA_TZ)

    def test_conversation_id_in_metadata(self) -> None:
        """parse() should include conversation_id from export in metadata."""
        conv = _minimal_conversation()
        conv["id"] = "conv-abc-123"
        raw = _make_raw_doc([conv])
        ingestor = ChatGPTIngestor()
        fragments = ingestor.parse(raw)
        assert fragments[0].metadata["conversation_id"] == "conv-abc-123"

    def test_conversation_id_absent_when_not_in_export(self) -> None:
        """parse() should omit conversation_id when not present in export."""
        conv = _minimal_conversation()
        # No "id" key in conv
        raw = _make_raw_doc([conv])
        ingestor = ChatGPTIngestor()
        fragments = ingestor.parse(raw)
        assert "conversation_id" not in fragments[0].metadata

    def test_turn_index_increments_for_multi_turn(self) -> None:
        """parse() should increment turn index for each fragment."""
        conv: dict[str, Any] = {
            "title": "Multi",
            "create_time": 1700042400.0,
            "mapping": {
                "root": {
                    "id": "root",
                    "message": None,
                    "parent": None,
                    "children": ["u1"],
                },
                "u1": {
                    "id": "u1",
                    "message": {
                        "id": "u1",
                        "author": {"role": "user"},
                        "content": {"content_type": "text", "parts": ["Q1"]},
                        "create_time": 1700042410.0,
                    },
                    "parent": "root",
                    "children": ["a1"],
                },
                "a1": {
                    "id": "a1",
                    "message": {
                        "id": "a1",
                        "author": {"role": "assistant"},
                        "content": {"content_type": "text", "parts": ["A1"]},
                        "create_time": 1700042420.0,
                    },
                    "parent": "u1",
                    "children": ["u2"],
                },
                "u2": {
                    "id": "u2",
                    "message": {
                        "id": "u2",
                        "author": {"role": "user"},
                        "content": {"content_type": "text", "parts": ["Q2"]},
                        "create_time": 1700042430.0,
                    },
                    "parent": "a1",
                    "children": ["a2"],
                },
                "a2": {
                    "id": "a2",
                    "message": {
                        "id": "a2",
                        "author": {"role": "assistant"},
                        "content": {"content_type": "text", "parts": ["A2"]},
                        "create_time": 1700042440.0,
                    },
                    "parent": "u2",
                    "children": [],
                },
            },
        }
        raw = _make_raw_doc([conv])
        ingestor = ChatGPTIngestor()
        fragments = ingestor.parse(raw)
        # Each pair splits into a human fragment and an AI fragment; the AI
        # title gets the ", AI" suffix and shares the human's turn index.
        assert [f.metadata["title"] for f in fragments] == [
            "Multi (turn 0)",
            "Multi (turn 0, AI)",
            "Multi (turn 1)",
            "Multi (turn 1, AI)",
        ]
        assert [f.metadata["author_role"] for f in fragments] == [
            "self",
            "ai",
            "self",
            "ai",
        ]

    def test_discover_non_directory_path(self, tmp_path: Path) -> None:
        """discover() should return empty list for non-directory path."""
        file_path = tmp_path / "not_a_dir.json"
        file_path.write_text("[]")
        ingestor = ChatGPTIngestor()
        docs = ingestor.discover(file_path)
        assert docs == []


# ---- convert_to_markdown() Tests ----


class TestChatGPTConvertToMarkdown:
    """Tests for ChatGPTIngestor.convert_to_markdown()."""

    def test_returns_string(self) -> None:
        """convert_to_markdown() should return a string."""
        ingestor = ChatGPTIngestor()
        fragment = ParsedFragment(
            content="**User**: Hello\n\n**Assistant**: World",
            metadata={"title": "Test", "platform": "chatgpt"},
            source_path="/fake/conv.json",
            timestamp=datetime(2024, 1, 15, 10, 0, 0, tzinfo=LA_TZ),
        )
        result = ingestor.convert_to_markdown(fragment)
        assert isinstance(result, str)

    def test_contains_title(self) -> None:
        """convert_to_markdown() should include the conversation title."""
        ingestor = ChatGPTIngestor()
        fragment = ParsedFragment(
            content="**User**: Hello\n\n**Assistant**: World",
            metadata={"title": "My Conversation", "platform": "chatgpt"},
            source_path="/fake/conv.json",
            timestamp=datetime(2024, 1, 15, 10, 0, 0, tzinfo=LA_TZ),
        )
        result = ingestor.convert_to_markdown(fragment)
        assert "My Conversation" in result

    def test_uses_blockquote_format(self) -> None:
        """convert_to_markdown() should format content with blockquotes."""
        ingestor = ChatGPTIngestor()
        fragment = ParsedFragment(
            content="**User**: Hello\n\n**Assistant**: World",
            metadata={"title": "Test", "platform": "chatgpt"},
            source_path="/fake/conv.json",
            timestamp=datetime(2024, 1, 15, 10, 0, 0, tzinfo=LA_TZ),
        )
        result = ingestor.convert_to_markdown(fragment)
        assert ">" in result

    def test_contains_content(self) -> None:
        """convert_to_markdown() should include the fragment content."""
        ingestor = ChatGPTIngestor()
        fragment = ParsedFragment(
            content="**User**: Test question\n\n**Assistant**: Test answer",
            metadata={"title": "Test", "platform": "chatgpt"},
            source_path="/fake/conv.json",
            timestamp=datetime(2024, 1, 15, 10, 0, 0, tzinfo=LA_TZ),
        )
        result = ingestor.convert_to_markdown(fragment)
        assert "Test question" in result
        assert "Test answer" in result


# ---- generate_frontmatter() Tests ----


class TestChatGPTGenerateFrontmatter:
    """Tests for ChatGPTIngestor.generate_frontmatter()."""

    def test_returns_dict(self) -> None:
        """generate_frontmatter() should return a dict."""
        ingestor = ChatGPTIngestor()
        fragment = ParsedFragment(
            content="content",
            metadata={"title": "Test", "platform": "chatgpt"},
            source_path="/fake/conv.json",
            timestamp=datetime(2024, 1, 15, 10, 0, 0, tzinfo=LA_TZ),
        )
        result = ingestor.generate_frontmatter(fragment)
        assert isinstance(result, dict)

    def test_has_source_platform(self) -> None:
        """Frontmatter should include source.platform as 'chatgpt'."""
        ingestor = ChatGPTIngestor()
        fragment = ParsedFragment(
            content="content",
            metadata={"title": "Test", "platform": "chatgpt"},
            source_path="/fake/conv.json",
            timestamp=datetime(2024, 1, 15, 10, 0, 0, tzinfo=LA_TZ),
        )
        result = ingestor.generate_frontmatter(fragment)
        assert result["source"]["platform"] == "chatgpt"

    def test_has_title(self) -> None:
        """Frontmatter should include the conversation title."""
        ingestor = ChatGPTIngestor()
        fragment = ParsedFragment(
            content="content",
            metadata={"title": "My Chat", "platform": "chatgpt"},
            source_path="/fake/conv.json",
            timestamp=datetime(2024, 1, 15, 10, 0, 0, tzinfo=LA_TZ),
        )
        result = ingestor.generate_frontmatter(fragment)
        assert result["title"] == "My Chat"

    def test_has_created_timestamp(self) -> None:
        """Frontmatter should include a created timestamp string."""
        ingestor = ChatGPTIngestor()
        fragment = ParsedFragment(
            content="content",
            metadata={"title": "Test", "platform": "chatgpt"},
            source_path="/fake/conv.json",
            timestamp=datetime(2024, 1, 15, 10, 0, 0, tzinfo=LA_TZ),
        )
        result = ingestor.generate_frontmatter(fragment)
        assert "created" in result
        assert isinstance(result["created"], str)

    def test_has_source_original_file(self) -> None:
        """Frontmatter source should include the original file path."""
        ingestor = ChatGPTIngestor()
        fragment = ParsedFragment(
            content="content",
            metadata={"title": "Test", "platform": "chatgpt"},
            source_path="/fake/conv.json",
            timestamp=datetime(2024, 1, 15, 10, 0, 0, tzinfo=LA_TZ),
        )
        result = ingestor.generate_frontmatter(fragment)
        assert result["source"]["original_file"] == "/fake/conv.json"

    def test_has_conversation_id_when_present(self) -> None:
        """Frontmatter source should include conversation_id when in metadata."""
        ingestor = ChatGPTIngestor()
        fragment = ParsedFragment(
            content="content",
            metadata={
                "title": "Test",
                "platform": "chatgpt",
                "conversation_id": "conv-xyz",
            },
            source_path="/fake/conv.json",
            timestamp=datetime(2024, 1, 15, 10, 0, 0, tzinfo=LA_TZ),
        )
        result = ingestor.generate_frontmatter(fragment)
        assert result["source"]["conversation_id"] == "conv-xyz"

    def test_omits_conversation_id_when_absent(self) -> None:
        """Frontmatter source should omit conversation_id when not in metadata."""
        ingestor = ChatGPTIngestor()
        fragment = ParsedFragment(
            content="content",
            metadata={"title": "Test", "platform": "chatgpt"},
            source_path="/fake/conv.json",
            timestamp=datetime(2024, 1, 15, 10, 0, 0, tzinfo=LA_TZ),
        )
        result = ingestor.generate_frontmatter(fragment)
        assert "conversation_id" not in result["source"]


class TestChatGPTAuthoredAt:
    """FEAT-031: per-turn ``authored_at`` from each message's ``create_time``.

    ChatGPT exports stamp every node in the mapping with a UTC
    Unix-epoch ``create_time``. Per-turn fragments must carry the
    user message's epoch so a 2023 conversation re-imported today
    is bucketed under 2023, not the import wall-clock.
    """

    def test_authored_at_from_user_message_create_time(self) -> None:
        """The user message's epoch becomes the fragment's ``authored_at``."""
        from datetime import UTC

        conv = _minimal_conversation(create_time=1700042400.0)
        # The user message create_time is conv.create_time + 10.0
        # = 1700042410.0
        ingestor = ChatGPTIngestor()
        fragments = ingestor.parse(_make_raw_doc([conv]))
        # One pair → human + AI fragment, both anchored to the user epoch.
        assert len(fragments) == 2
        for frag in fragments:
            authored = frag.metadata["authored_at"]
            assert authored is not None
            assert authored == datetime.fromtimestamp(1700042410.0, tz=UTC)
            # UTC, not LA — preserves the source's instant honestly.
            assert authored.tzinfo == UTC

    def test_authored_at_falls_back_to_conversation_create_time(self) -> None:
        """No per-message ``create_time`` → conversation-level epoch wins.

        A user message without ``create_time`` (some older exports
        omit it) still anchors to the conversation's ``create_time``
        rather than going to ``None`` — the conversation's epoch is
        the next-best honest source date.
        """
        from datetime import UTC

        conv = _minimal_conversation(create_time=1700042400.0)
        # Strip the user message's create_time.
        conv["mapping"]["u1"]["message"].pop("create_time")
        ingestor = ChatGPTIngestor()
        fragments = ingestor.parse(_make_raw_doc([conv]))
        authored = fragments[0].metadata["authored_at"]
        assert authored is not None
        assert authored == datetime.fromtimestamp(1700042400.0, tz=UTC)

    def test_authored_at_none_when_no_create_time_anywhere(self) -> None:
        """Both conv- and msg-level missing → ``authored_at`` is ``None``.

        FEAT-031 forbids guessing — the honest answer is ``None`` so
        downstream time-bucket surfaces fall through to ``ingested``.
        """
        conv = _minimal_conversation(create_time=0.0)
        # Wipe the user message's create_time too.
        conv["mapping"]["u1"]["message"]["create_time"] = 0.0
        ingestor = ChatGPTIngestor()
        fragments = ingestor.parse(_make_raw_doc([conv]))
        authored = fragments[0].metadata["authored_at"]
        assert authored is None

    def test_authored_at_in_generated_frontmatter(self) -> None:
        """``generate_frontmatter`` surfaces ``authored_at`` as ISO string."""
        conv = _minimal_conversation(create_time=1700042400.0)
        ingestor = ChatGPTIngestor()
        fragments = ingestor.parse(_make_raw_doc([conv]))
        fm = ingestor.generate_frontmatter(fragments[0])
        assert "authored_at" in fm
        assert fm["authored_at"].startswith("2023-11-15")

    def test_no_authored_at_omits_key_from_frontmatter(self) -> None:
        """When extraction yields ``None`` the key is absent (terse YAML)."""
        conv = _minimal_conversation(create_time=0.0)
        conv["mapping"]["u1"]["message"]["create_time"] = 0.0
        ingestor = ChatGPTIngestor()
        fragments = ingestor.parse(_make_raw_doc([conv]))
        fm = ingestor.generate_frontmatter(fragments[0])
        assert "authored_at" not in fm


# ---- Registry Tests ----


class TestChatGPTRegistry:
    """Tests for ChatGPTIngestor registration in INGESTOR_REGISTRY."""

    def test_registered_in_registry(self) -> None:
        """ChatGPTIngestor should be registered in INGESTOR_REGISTRY."""
        from creek.ingest import INGESTOR_REGISTRY

        assert "chatgpt" in INGESTOR_REGISTRY

    def test_registry_maps_to_class(self) -> None:
        """Registry entry should map to the ChatGPTIngestor class."""
        from creek.ingest import INGESTOR_REGISTRY

        assert INGESTOR_REGISTRY["chatgpt"] is ChatGPTIngestor


# ---- Full Pipeline Integration Test ----


class TestChatGPTIngestPipeline:
    """Tests for the full ingest() pipeline with ChatGPTIngestor."""

    def test_full_ingest_from_fixture(self, tmp_path: Path) -> None:
        """Full ingest pipeline should work with fixture data."""
        fixture_path = FIXTURES / "sample_chatgpt_export.json"
        # Copy fixture to tmp directory
        dest = tmp_path / "conversations.json"
        dest.write_bytes(fixture_path.read_bytes())

        ingestor = ChatGPTIngestor()
        result = ingestor.ingest(tmp_path)

        assert isinstance(result, IngestResult)
        assert len(result.fragments) > 0
        assert len(result.provenance) > 0
        assert result.errors == []

    def test_ingest_empty_directory(self, tmp_path: Path) -> None:
        """ingest() on an empty directory should return an empty result."""
        ingestor = ChatGPTIngestor()
        result = ingestor.ingest(tmp_path)
        assert isinstance(result, IngestResult)
        assert result.fragments == []


# ---- Robustness Tests ----


class TestLinearizeTreeCycleGuard:
    """Tests that _linearize_tree handles cycles in malformed data."""

    def test_cycle_does_not_loop_forever(self) -> None:
        """_linearize_tree should terminate on a mapping with a cycle."""
        from creek.ingest.chatgpt import _linearize_tree

        # A -> B -> C -> A (cycle back to root)
        mapping: dict[str, Any] = {
            "a": {
                "parent": None,
                "children": ["b"],
                "message": {"author": {"role": "user"}, "content": {"parts": ["Hi"]}},
            },
            "b": {
                "parent": "a",
                "children": ["c"],
                "message": {
                    "author": {"role": "assistant"},
                    "content": {"parts": ["Hello"]},
                },
            },
            "c": {
                "parent": "b",
                "children": ["a"],  # cycle back to root
                "message": {
                    "author": {"role": "user"},
                    "content": {"parts": ["Again"]},
                },
            },
        }
        messages = _linearize_tree(mapping)
        # Should get exactly 3 messages (a, b, c) and stop at cycle
        assert len(messages) == 3


class TestCountDescendantsDeepTree:
    """Tests that _count_descendants handles deep trees without recursion errors."""

    def test_deep_tree_does_not_hit_recursion_limit(self) -> None:
        """_count_descendants works on trees deeper than recursion limit."""
        from creek.ingest.chatgpt import _count_descendants

        depth = 2000  # well beyond default recursion limit of ~1000
        mapping: dict[str, Any] = {}
        for i in range(depth):
            node_id = f"node_{i}"
            child_id = f"node_{i + 1}" if i < depth - 1 else None
            mapping[node_id] = {
                "parent": f"node_{i - 1}" if i > 0 else None,
                "children": [child_id] if child_id else [],
                "message": None,
            }

        count = _count_descendants("node_0", mapping)
        assert count == depth


class TestLinearizeTreeParentOnlyMapping:
    """_linearize_tree reconstructs children from parent pointers (#592).

    Current ChatGPT exports give nodes ``{id, message, parent}`` with no
    ``children`` arrays. The walk must rebuild children from parents rather
    than yielding 0 messages.
    """

    def test_parent_only_mapping_linearizes_in_order(self) -> None:
        """A parent-only mapping still linearizes system -> user -> assistant."""
        from creek.ingest.chatgpt import _linearize_tree

        conv = _minimal_conversation()
        mapping = conv["mapping"]
        for node in mapping.values():
            node.pop("children", None)  # mimic current export (parent-only)

        ordered = _linearize_tree(mapping)

        roles = [m["author"]["role"] for m in ordered]
        assert roles == ["system", "user", "assistant"]

    def test_parent_only_branch_picks_longest(self) -> None:
        """With reconstructed children, the longest branch still wins."""
        from creek.ingest.chatgpt import _linearize_tree

        mapping: dict[str, Any] = {
            "root": {"id": "root", "message": None, "parent": None},
            "u1": {
                "id": "u1",
                "parent": "root",
                "message": {
                    "author": {"role": "user"},
                    "content": {"parts": ["Q"]},
                    "create_time": 1.0,
                },
            },
            "a1": {
                "id": "a1",
                "parent": "u1",
                "message": {
                    "author": {"role": "assistant"},
                    "content": {"parts": ["short"]},
                    "create_time": 2.0,
                },
            },
            "a2": {
                "id": "a2",
                "parent": "u1",
                "message": {
                    "author": {"role": "assistant"},
                    "content": {"parts": ["long"]},
                    "create_time": 3.0,
                },
            },
            "a3": {
                "id": "a3",
                "parent": "a2",
                "message": {
                    "author": {"role": "user"},
                    "content": {"parts": ["follow"]},
                    "create_time": 4.0,
                },
            },
        }

        ordered = _linearize_tree(mapping)

        texts = [m["content"]["parts"][0] for m in ordered]
        assert texts == ["Q", "long", "follow"]


def _chain_conversation(
    turns: list[tuple[str, str]],
    title: str = "Consecutive Chat",
    create_time: float = 1700042400.0,
) -> dict[str, Any]:
    """Build a ChatGPT conversation whose mapping is one linear chain.

    Mirrors the node shape of :func:`_minimal_conversation` (root -> ... ->
    leaf, each node carrying an ``author.role`` and text ``parts``) but lets
    a test state the exact role sequence it needs.

    Args:
        turns: Ordered ``(role, text)`` pairs, one message node each.
        title: Conversation title.
        create_time: Unix epoch for the conversation; each node is stamped
            ten seconds after its predecessor.

    Returns:
        A dict representing one ChatGPT conversation.
    """
    mapping: dict[str, Any] = {
        "root": {"id": "root", "message": None, "parent": None, "children": []},
    }
    parent = "root"
    for idx, (role, text) in enumerate(turns):
        node_id = f"n{idx}"
        mapping[parent]["children"] = [node_id]
        mapping[node_id] = {
            "id": node_id,
            "message": {
                "id": node_id,
                "author": {"role": role},
                "content": {"content_type": "text", "parts": [text]},
                "create_time": create_time + 10.0 * (idx + 1),
            },
            "parent": parent,
            "children": [],
        }
        parent = node_id
    return {
        "title": title,
        "create_time": create_time,
        "update_time": create_time + 100.0,
        "mapping": mapping,
    }


class TestChatGPTConsecutiveMessages:
    """Runs of same-role messages must all reach a fragment (issue #1333).

    The old index walk paired a user message only with a strictly adjacent
    assistant message, so the first of two consecutive user messages was
    skipped, the second of two consecutive assistant messages was dropped,
    and a system node between the turns broke the pair entirely — even
    though system messages are documented as skipped.
    """

    def test_consecutive_user_messages_merge_into_human_fragment(self) -> None:
        """Both user messages of a run land in the human fragment."""
        conv = _chain_conversation(
            [
                ("user", "Part one"),
                ("user", "Part two"),
                ("assistant", "Reply"),
            ]
        )
        ingestor = ChatGPTIngestor()
        fragments = ingestor.parse(_make_raw_doc([conv]))

        assert len(fragments) == 2
        assert fragments[0].metadata["author_role"] == "self"
        # Both halves of the operator's question, separated by a blank line.
        assert fragments[0].content == "Part one\n\nPart two"
        assert fragments[1].content == "Reply"

    def test_consecutive_assistant_messages_merge_into_ai_fragment(self) -> None:
        """Both assistant messages of a run land in the AI fragment."""
        conv = _chain_conversation(
            [
                ("user", "Question"),
                ("assistant", "A one"),
                ("assistant", "A two"),
            ]
        )
        ingestor = ChatGPTIngestor()
        fragments = ingestor.parse(_make_raw_doc([conv]))

        assert len(fragments) == 2
        assert fragments[0].content == "Question"
        assert fragments[1].metadata["author_role"] == "ai"
        assert fragments[1].content == "A one\n\nA two"

    def test_system_message_between_turns_does_not_break_the_pair(self) -> None:
        """A system node mid-conversation is skipped, not a pair divider."""
        conv = _chain_conversation(
            [
                ("user", "Question"),
                ("system", "System prompt."),
                ("assistant", "Reply"),
            ]
        )
        ingestor = ChatGPTIngestor()
        fragments = ingestor.parse(_make_raw_doc([conv]))

        # Before #1333 this yielded zero fragments: the system node broke
        # strict adjacency and the whole exchange was dropped.
        assert len(fragments) == 2
        assert fragments[0].content == "Question"
        assert fragments[1].content == "Reply"
        assert "System prompt." not in fragments[0].content
        assert "System prompt." not in fragments[1].content
        assert [f.metadata["author_role"] for f in fragments] == ["self", "ai"]
