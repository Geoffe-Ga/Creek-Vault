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


# ---- parse() Tests ----


class TestChatGPTParse:
    """Tests for ChatGPTIngestor.parse()."""

    def test_parses_single_conversation(self) -> None:
        """parse() should extract fragments from a single conversation."""
        conv = _minimal_conversation()
        raw = _make_raw_doc([conv])
        ingestor = ChatGPTIngestor()
        fragments = ingestor.parse(raw)
        assert len(fragments) == 1

    def test_parses_multiple_conversations(self) -> None:
        """parse() should handle multiple conversations in one file."""
        conv1 = _minimal_conversation(title="Chat 1", create_time=1700042400.0)
        conv2 = _minimal_conversation(title="Chat 2", create_time=1700128800.0)
        raw = _make_raw_doc([conv1, conv2])
        ingestor = ChatGPTIngestor()
        fragments = ingestor.parse(raw)
        assert len(fragments) == 2

    def test_fragment_content_has_user_and_assistant(self) -> None:
        """parse() should pair user and assistant messages in fragments."""
        conv = _minimal_conversation()
        raw = _make_raw_doc([conv])
        ingestor = ChatGPTIngestor()
        fragments = ingestor.parse(raw)
        assert "Hello!" in fragments[0].content
        assert "Hi there!" in fragments[0].content

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

    def test_fragment_metadata_has_title(self) -> None:
        """parse() should include conversation title in fragment metadata."""
        conv = _minimal_conversation(title="My Chat")
        raw = _make_raw_doc([conv])
        ingestor = ChatGPTIngestor()
        fragments = ingestor.parse(raw)
        assert fragments[0].metadata["title"] == "My Chat"

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
        # Two user+assistant pairs = two fragments
        assert len(fragments) == 2
        assert "First question" in fragments[0].content
        assert "First answer" in fragments[0].content
        assert "Second question" in fragments[1].content
        assert "Second answer" in fragments[1].content

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
        # Fixture has 2 conversations: first has 2 pairs, second has 1 pair
        assert len(fragments) == 3


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
        # Should follow the long branch (2 pairs), not short branch (1 pair)
        assert len(fragments) == 2
        all_content = " ".join(f.content for f in fragments)
        assert "Long branch answer." in all_content
        assert "Story continues here." in all_content


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
        assert len(fragments) == 1

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
        # Empty content pair should still produce a fragment (empty content)
        assert len(fragments) == 1

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
        assert len(fragments) == 1

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

    def test_missing_create_time_uses_fallback(self) -> None:
        """parse() should handle conversations with no create_time."""
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
        assert len(fragments) == 1
        # Should have a valid LA timezone timestamp (fallback)
        assert str(fragments[0].timestamp.tzinfo) == "America/Los_Angeles"

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
