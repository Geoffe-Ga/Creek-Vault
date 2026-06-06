"""Tests for creek.clean.filters.chatbot — chatbot pre-ingestion filter.

Tests cover:
- MessageVerdict and ConversationFilterResult model structure
- ChatbotFilterConfig defaults and customization
- System prompt filtering (role=system, tool-definition preambles)
- Tool call output filtering (Bash output, file contents, search results)
- Regeneration collapse (keep only final consecutive assistant response)
- Abandoned conversation detection (≤2 turns, no substantive content)
- Minimum human turn length filtering
- Code-only response flagging (>90% code blocks)
- ChatGPT branch selection (follow longest branch)
- Integration with both Claude and ChatGPT message structures
- Configurable thresholds
- Ingestor integration (ClaudeIngestor and ChatGPTIngestor)
"""

from __future__ import annotations

import json
from typing import Any

from creek.clean.filters.chatbot import (
    ChatbotFilter,
    ChatbotFilterConfig,
    ConversationFilterResult,
    MessageVerdict,
)

# ---------------------------------------------------------------------------
# MessageVerdict model
# ---------------------------------------------------------------------------


class TestMessageVerdict:
    """Tests for the MessageVerdict model."""

    def test_required_fields(self) -> None:
        """MessageVerdict should have action and reason fields."""
        verdict = MessageVerdict(action="keep", reason="substantive content")
        assert verdict.action == "keep"
        assert verdict.reason == "substantive content"

    def test_action_values(self) -> None:
        """Action should accept 'keep', 'skip', and 'flag'."""
        for action in ("keep", "skip", "flag"):
            verdict = MessageVerdict(action=action, reason="test")
            assert verdict.action == action


# ---------------------------------------------------------------------------
# ConversationFilterResult model
# ---------------------------------------------------------------------------


class TestConversationFilterResult:
    """Tests for the ConversationFilterResult model."""

    def test_required_fields(self) -> None:
        """ConversationFilterResult should have all expected fields."""
        result = ConversationFilterResult(
            messages=[{"role": "human", "content": "hello"}],
            skipped=1,
            flagged=0,
            reasons=["filtered system prompt"],
            is_abandoned=False,
        )
        assert len(result.messages) == 1
        assert result.skipped == 1
        assert result.flagged == 0
        assert result.is_abandoned is False

    def test_empty_conversation(self) -> None:
        """Empty conversation should be representable."""
        result = ConversationFilterResult(
            messages=[],
            skipped=0,
            flagged=0,
            reasons=[],
            is_abandoned=True,
        )
        assert result.messages == []
        assert result.is_abandoned is True


# ---------------------------------------------------------------------------
# ChatbotFilterConfig
# ---------------------------------------------------------------------------


class TestChatbotFilterConfig:
    """Tests for the ChatbotFilterConfig model."""

    def test_default_values(self) -> None:
        """Config should have sensible defaults."""
        config = ChatbotFilterConfig()
        assert config.min_human_turn_length == 20
        assert config.code_block_threshold == 0.9
        assert config.max_abandoned_turns == 2
        assert config.skip_system_prompts is True
        assert config.skip_tool_outputs is True
        assert config.collapse_regenerations is True

    def test_custom_values(self) -> None:
        """Config should accept custom values."""
        config = ChatbotFilterConfig(
            min_human_turn_length=50,
            code_block_threshold=0.8,
            max_abandoned_turns=3,
            skip_system_prompts=False,
        )
        assert config.min_human_turn_length == 50
        assert config.code_block_threshold == 0.8
        assert config.max_abandoned_turns == 3
        assert config.skip_system_prompts is False


# ---------------------------------------------------------------------------
# ChatbotFilter — System prompt filtering
# ---------------------------------------------------------------------------


class TestFilterSystemPrompts:
    """Tests for filtering system prompts."""

    def test_skip_role_system_claude(self) -> None:
        """Messages with role=system should be skipped (Claude format)."""
        f = ChatbotFilter()
        messages = [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "human", "content": "What is Python?"},
            {"role": "assistant", "content": "Python is a programming language."},
        ]
        result = f.filter_conversation(messages, platform="claude")
        assert result.skipped >= 1
        roles = [m["role"] for m in result.messages]
        assert "system" not in roles

    def test_skip_role_system_chatgpt(self) -> None:
        """Messages with role=system should be skipped (ChatGPT format)."""
        f = ChatbotFilter()
        messages = [
            {"role": "system", "content": "You are ChatGPT."},
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi there!"},
        ]
        result = f.filter_conversation(messages, platform="chatgpt")
        roles = [m["role"] for m in result.messages]
        assert "system" not in roles

    def test_skip_tool_definition_preamble(self) -> None:
        """Messages containing tool-definition preambles should be skipped."""
        f = ChatbotFilter()
        messages = [
            {
                "role": "system",
                "content": (
                    "You have access to the following tools:\n"
                    "- bash: Execute shell commands\n"
                    "- python: Run Python code"
                ),
            },
            {"role": "human", "content": "Help me with code"},
            {"role": "assistant", "content": "Sure, I can help."},
        ]
        result = f.filter_conversation(messages, platform="claude")
        assert result.skipped >= 1

    def test_keep_system_prompts_when_disabled(self) -> None:
        """System prompts should be kept when filtering is disabled."""
        config = ChatbotFilterConfig(skip_system_prompts=False)
        f = ChatbotFilter(config=config)
        messages = [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "human", "content": "Hello"},
            {"role": "assistant", "content": "Hi!"},
        ]
        result = f.filter_conversation(messages, platform="claude")
        roles = [m["role"] for m in result.messages]
        assert "system" in roles


# ---------------------------------------------------------------------------
# ChatbotFilter — Tool call output filtering
# ---------------------------------------------------------------------------


class TestFilterToolOutputs:
    """Tests for filtering tool call outputs."""

    def test_skip_tool_role_messages(self) -> None:
        """Messages with role=tool should be skipped."""
        f = ChatbotFilter()
        messages = [
            {"role": "human", "content": "Run ls"},
            {"role": "assistant", "content": "Let me run that."},
            {"role": "tool", "content": "file1.txt\nfile2.txt"},
            {"role": "assistant", "content": "I found two files."},
        ]
        result = f.filter_conversation(messages, platform="claude")
        roles = [m["role"] for m in result.messages]
        assert "tool" not in roles

    def test_skip_tool_result_role(self) -> None:
        """Messages with role=tool_result should be skipped."""
        f = ChatbotFilter()
        messages = [
            {"role": "human", "content": "Search for files"},
            {"role": "assistant", "content": "Searching..."},
            {"role": "tool_result", "content": "Found 5 results"},
        ]
        result = f.filter_conversation(messages, platform="claude")
        roles = [m["role"] for m in result.messages]
        assert "tool_result" not in roles

    def test_keep_tool_outputs_when_disabled(self) -> None:
        """Tool outputs should be kept when filtering is disabled."""
        config = ChatbotFilterConfig(skip_tool_outputs=False)
        f = ChatbotFilter(config=config)
        messages = [
            {"role": "human", "content": "Run a command"},
            {"role": "tool", "content": "output here"},
        ]
        result = f.filter_conversation(messages, platform="claude")
        roles = [m["role"] for m in result.messages]
        assert "tool" in roles


# ---------------------------------------------------------------------------
# ChatbotFilter — Regeneration collapse
# ---------------------------------------------------------------------------


class TestRegenerationCollapse:
    """Tests for collapsing regenerated assistant responses."""

    def test_collapse_consecutive_assistant_messages(self) -> None:
        """Only the last of consecutive assistant messages should be kept."""
        f = ChatbotFilter()
        messages = [
            {"role": "human", "content": "Write a poem"},
            {"role": "assistant", "content": "First draft of poem..."},
            {"role": "assistant", "content": "Second draft of poem..."},
            {"role": "assistant", "content": "Final polished poem here."},
        ]
        result = f.filter_conversation(messages, platform="claude")
        assistant_msgs = [m for m in result.messages if m["role"] == "assistant"]
        assert len(assistant_msgs) == 1
        assert assistant_msgs[0]["content"] == "Final polished poem here."

    def test_no_collapse_separated_assistant_messages(self) -> None:
        """Non-consecutive assistant messages should all be kept."""
        f = ChatbotFilter()
        messages = [
            {"role": "human", "content": "What is my first question here?"},
            {"role": "assistant", "content": "Answer 1"},
            {"role": "human", "content": "What is my second question?"},
            {"role": "assistant", "content": "Answer 2"},
        ]
        result = f.filter_conversation(messages, platform="claude")
        assistant_msgs = [m for m in result.messages if m["role"] == "assistant"]
        assert len(assistant_msgs) == 2

    def test_collapse_disabled(self) -> None:
        """Consecutive assistant messages should be kept when collapse is disabled."""
        config = ChatbotFilterConfig(collapse_regenerations=False)
        f = ChatbotFilter(config=config)
        messages = [
            {"role": "human", "content": "Write a poem"},
            {"role": "assistant", "content": "Draft 1"},
            {"role": "assistant", "content": "Draft 2"},
        ]
        result = f.filter_conversation(messages, platform="claude")
        assistant_msgs = [m for m in result.messages if m["role"] == "assistant"]
        assert len(assistant_msgs) == 2

    def test_collapse_counts_skipped(self) -> None:
        """Collapsed regenerations should be counted as skipped."""
        f = ChatbotFilter()
        messages = [
            {"role": "human", "content": "Write something"},
            {"role": "assistant", "content": "Draft 1"},
            {"role": "assistant", "content": "Draft 2"},
            {"role": "assistant", "content": "Final"},
        ]
        result = f.filter_conversation(messages, platform="claude")
        assert result.skipped >= 2


# ---------------------------------------------------------------------------
# ChatbotFilter — Abandoned conversation detection
# ---------------------------------------------------------------------------


class TestAbandonedConversation:
    """Tests for detecting abandoned conversations."""

    def test_flag_abandoned_with_one_turn(self) -> None:
        """Conversation with ≤2 turns and no substance should be flagged."""
        f = ChatbotFilter()
        messages = [
            {"role": "human", "content": "hi"},
            {"role": "assistant", "content": "Hello!"},
        ]
        result = f.filter_conversation(messages, platform="claude")
        assert result.is_abandoned is True

    def test_not_abandoned_with_substance(self) -> None:
        """Conversation with substantive content should not be flagged."""
        f = ChatbotFilter()
        messages = [
            {"role": "human", "content": "Explain quantum computing in detail please."},
            {
                "role": "assistant",
                "content": (
                    "Quantum computing leverages quantum mechanical phenomena "
                    "such as superposition and entanglement to process information."
                ),
            },
        ]
        result = f.filter_conversation(messages, platform="claude")
        assert result.is_abandoned is False

    def test_not_abandoned_with_many_turns(self) -> None:
        """Conversation with more than max_abandoned_turns should not be flagged."""
        f = ChatbotFilter()
        messages = [
            {"role": "human", "content": "Hello, how are you doing today?"},
            {"role": "assistant", "content": "Hello! I'm doing well, thank you."},
            {"role": "human", "content": "Can you tell me about Python?"},
            {"role": "assistant", "content": "Python is a programming language."},
            {"role": "human", "content": "That sounds very interesting indeed."},
            {"role": "assistant", "content": "Glad you think so! It's very versatile."},
        ]
        result = f.filter_conversation(messages, platform="claude")
        assert result.is_abandoned is False

    def test_custom_abandoned_threshold(self) -> None:
        """Custom max_abandoned_turns should be respected."""
        config = ChatbotFilterConfig(max_abandoned_turns=4)
        f = ChatbotFilter(config=config)
        messages = [
            {"role": "human", "content": "hi"},
            {"role": "assistant", "content": "Hello!"},
            {"role": "human", "content": "bye"},
            {"role": "assistant", "content": "Goodbye!"},
        ]
        result = f.filter_conversation(messages, platform="claude")
        assert result.is_abandoned is True


# ---------------------------------------------------------------------------
# ChatbotFilter — Minimum human turn length
# ---------------------------------------------------------------------------


class TestMinHumanTurnLength:
    """Tests for filtering short human turns."""

    def test_skip_short_human_turn(self) -> None:
        """Human turns shorter than threshold should be skipped."""
        f = ChatbotFilter()
        messages = [
            {"role": "human", "content": "ok"},
            {"role": "assistant", "content": "Is there anything else?"},
            {"role": "human", "content": "Tell me about machine learning algorithms."},
            {"role": "assistant", "content": "Machine learning is a field of AI."},
        ]
        result = f.filter_conversation(messages, platform="claude")
        human_msgs = [m for m in result.messages if m["role"] == "human"]
        assert len(human_msgs) == 1
        assert "machine learning" in human_msgs[0]["content"].lower()

    def test_custom_min_length(self) -> None:
        """Custom min_human_turn_length should be respected."""
        config = ChatbotFilterConfig(min_human_turn_length=5)
        f = ChatbotFilter(config=config)
        messages = [
            {"role": "human", "content": "ok"},
            {"role": "assistant", "content": "Sure thing."},
            {"role": "human", "content": "Hello there friend"},
            {"role": "assistant", "content": "Hi!"},
        ]
        result = f.filter_conversation(messages, platform="claude")
        human_msgs = [m for m in result.messages if m["role"] == "human"]
        assert len(human_msgs) == 1
        assert human_msgs[0]["content"] == "Hello there friend"

    def test_keep_all_when_min_length_zero(self) -> None:
        """All human turns should be kept when min length is 0."""
        config = ChatbotFilterConfig(min_human_turn_length=0)
        f = ChatbotFilter(config=config)
        messages = [
            {"role": "human", "content": "y"},
            {"role": "assistant", "content": "Yes?"},
        ]
        result = f.filter_conversation(messages, platform="claude")
        human_msgs = [m for m in result.messages if m["role"] == "human"]
        assert len(human_msgs) == 1


# ---------------------------------------------------------------------------
# ChatbotFilter — Code-only response flagging
# ---------------------------------------------------------------------------


class TestCodeOnlyFlagging:
    """Tests for flagging code-only assistant responses."""

    def test_flag_code_only_response(self) -> None:
        """Assistant responses that are >90% code should be flagged."""
        f = ChatbotFilter()
        code_block = "```python\n" + "x = 1\n" * 50 + "```"
        messages = [
            {"role": "human", "content": "Write a Python script for me please."},
            {"role": "assistant", "content": code_block},
        ]
        result = f.filter_conversation(messages, platform="claude")
        assert result.flagged >= 1

    def test_keep_mixed_code_response(self) -> None:
        """Responses with code and explanation should not be flagged."""
        f = ChatbotFilter()
        content = (
            "Here's how to print in Python:\n\n"
            "```python\nprint('hello')\n```\n\n"
            "This uses the built-in print function to output text to stdout. "
            "The string is passed as an argument and displayed on the console."
        )
        messages = [
            {"role": "human", "content": "How do I print in Python?"},
            {"role": "assistant", "content": content},
        ]
        result = f.filter_conversation(messages, platform="claude")
        assert result.flagged == 0

    def test_custom_code_threshold(self) -> None:
        """Custom code_block_threshold should be respected."""
        config = ChatbotFilterConfig(code_block_threshold=0.5)
        f = ChatbotFilter(config=config)
        content = "```\ncode here\nmore code\n```\nbrief note"
        messages = [
            {"role": "human", "content": "Show me some code please now."},
            {"role": "assistant", "content": content},
        ]
        result = f.filter_conversation(messages, platform="claude")
        assert result.flagged >= 1


# ---------------------------------------------------------------------------
# ChatbotFilter — ChatGPT-specific handling
# ---------------------------------------------------------------------------


class TestChatGPTSpecific:
    """Tests for ChatGPT-specific message structures."""

    def test_chatgpt_user_role(self) -> None:
        """ChatGPT uses 'user' role instead of 'human'."""
        f = ChatbotFilter()
        messages = [
            {"role": "user", "content": "What is the meaning of life and everything?"},
            {
                "role": "assistant",
                "content": "That's a philosophical question with many answers.",
            },
        ]
        result = f.filter_conversation(messages, platform="chatgpt")
        assert len(result.messages) == 2

    def test_chatgpt_tool_message(self) -> None:
        """ChatGPT tool messages should be filtered."""
        f = ChatbotFilter()
        messages = [
            {"role": "user", "content": "Search for something interesting now."},
            {"role": "assistant", "content": "Let me search."},
            {"role": "tool", "content": "Search results here..."},
            {"role": "assistant", "content": "I found some results."},
        ]
        result = f.filter_conversation(messages, platform="chatgpt")
        roles = [m["role"] for m in result.messages]
        assert "tool" not in roles


# ---------------------------------------------------------------------------
# ChatbotFilter — Content extraction helpers
# ---------------------------------------------------------------------------


class TestContentExtraction:
    """Tests for extracting text from various message content formats."""

    def test_string_content(self) -> None:
        """Plain string content should be extracted directly."""
        f = ChatbotFilter()
        messages = [
            {"role": "human", "content": "Tell me about Python programming language."},
            {"role": "assistant", "content": "Python is a versatile language."},
        ]
        result = f.filter_conversation(messages, platform="claude")
        assert len(result.messages) == 2

    def test_list_content_claude(self) -> None:
        """Claude list-format content should be handled."""
        f = ChatbotFilter()
        messages = [
            {
                "role": "human",
                "content": [
                    {"type": "text", "text": "Explain this concept in detail."},
                ],
            },
            {"role": "assistant", "content": "Sure, let me explain the concept."},
        ]
        result = f.filter_conversation(messages, platform="claude")
        assert len(result.messages) == 2

    def test_chatgpt_parts_content(self) -> None:
        """ChatGPT parts-format content should be handled."""
        f = ChatbotFilter()
        messages = [
            {
                "role": "user",
                "content": {
                    "parts": [
                        "Tell me about artificial intelligence please.",
                    ],
                },
            },
            {"role": "assistant", "content": "AI is a broad field of study."},
        ]
        result = f.filter_conversation(messages, platform="chatgpt")
        assert len(result.messages) == 2

    def test_empty_content_handling(self) -> None:
        """Messages with empty content should be handled gracefully."""
        f = ChatbotFilter()
        messages = [
            {"role": "human", "content": ""},
            {"role": "assistant", "content": "I didn't catch that."},
        ]
        result = f.filter_conversation(messages, platform="claude")
        # Empty human turn should be skipped (below min length)
        human_msgs = [m for m in result.messages if m["role"] == "human"]
        assert len(human_msgs) == 0


# ---------------------------------------------------------------------------
# ChatbotFilter — Combined filtering
# ---------------------------------------------------------------------------


class TestCombinedFiltering:
    """Tests for multiple filters working together."""

    def test_all_filters_applied(self) -> None:
        """All filter rules should be applied in a single pass."""
        f = ChatbotFilter()
        messages = [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "human", "content": "ok"},
            {"role": "assistant", "content": "What can I help with?"},
            {
                "role": "human",
                "content": "Write me a detailed Python sorting algorithm.",
            },
            {"role": "assistant", "content": "Here's a first attempt..."},
            {"role": "assistant", "content": "Actually, here's a better version."},
            {"role": "tool", "content": "execution result"},
            {"role": "assistant", "content": "The sorting algorithm works correctly."},
        ]
        result = f.filter_conversation(messages, platform="claude")

        roles = [m["role"] for m in result.messages]
        assert "system" not in roles
        assert "tool" not in roles
        assert result.skipped >= 3  # system + short human + regeneration + tool

    def test_filter_preserves_message_order(self) -> None:
        """Filtered messages should maintain their original order."""
        f = ChatbotFilter()
        messages = [
            {"role": "system", "content": "System prompt."},
            {"role": "human", "content": "First question about programming here."},
            {"role": "assistant", "content": "First answer about programming here."},
            {"role": "human", "content": "Second question about something else."},
            {"role": "assistant", "content": "Second answer about something else."},
        ]
        result = f.filter_conversation(messages, platform="claude")
        contents = [m["content"] for m in result.messages]
        assert contents[0] == "First question about programming here."
        assert contents[-1] == "Second answer about something else."

    def test_reasons_populated(self) -> None:
        """Filter reasons should explain what was filtered."""
        f = ChatbotFilter()
        messages = [
            {"role": "system", "content": "You are helpful."},
            {"role": "human", "content": "hi"},
            {"role": "assistant", "content": "Hello!"},
        ]
        result = f.filter_conversation(messages, platform="claude")
        assert len(result.reasons) > 0


# ---------------------------------------------------------------------------
# ChatbotFilter — Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    """Tests for edge cases and boundary conditions."""

    def test_empty_message_list(self) -> None:
        """Empty message list should return empty result."""
        f = ChatbotFilter()
        result = f.filter_conversation([], platform="claude")
        assert result.messages == []
        assert result.skipped == 0
        assert result.is_abandoned is True

    def test_all_messages_filtered(self) -> None:
        """When all messages are filtered, result should be empty."""
        f = ChatbotFilter()
        messages = [
            {"role": "system", "content": "System prompt."},
            {"role": "tool", "content": "Tool output."},
        ]
        result = f.filter_conversation(messages, platform="claude")
        assert result.messages == []

    def test_single_human_message(self) -> None:
        """Single human message without response should be handled."""
        f = ChatbotFilter()
        messages = [
            {"role": "human", "content": "This is a question that got no answer back."},
        ]
        result = f.filter_conversation(messages, platform="claude")
        assert len(result.messages) == 1

    def test_unknown_role_kept(self) -> None:
        """Messages with unknown roles should be kept by default."""
        f = ChatbotFilter()
        messages = [
            {"role": "human", "content": "Tell me about something interesting."},
            {"role": "custom_role", "content": "Some custom content here."},
            {"role": "assistant", "content": "Here's my response to you."},
        ]
        result = f.filter_conversation(messages, platform="claude")
        roles = [m["role"] for m in result.messages]
        assert "custom_role" in roles

    def test_missing_content_key(self) -> None:
        """Messages without content key should be handled gracefully."""
        f = ChatbotFilter()
        messages = [
            {"role": "human"},
            {"role": "assistant", "content": "Response to empty message here."},
        ]
        result = f.filter_conversation(messages, platform="claude")
        # Human message with no content treated as empty → skipped
        assert len(result.messages) >= 1

    def test_none_content(self) -> None:
        """Messages with None content should be handled gracefully."""
        f = ChatbotFilter()
        messages = [
            {"role": "human", "content": None},
            {"role": "assistant", "content": "I can help with that."},
        ]
        result = f.filter_conversation(messages, platform="claude")
        # None content treated as empty → skipped
        human_msgs = [m for m in result.messages if m["role"] == "human"]
        assert len(human_msgs) == 0


# ---------------------------------------------------------------------------
# Integration — ClaudeIngestor with ChatbotFilter
# ---------------------------------------------------------------------------


def _make_claude_export(conversations: list[dict[str, Any]]) -> bytes:
    """Build a Claude-format JSON export as bytes."""
    return json.dumps({"conversations": conversations}).encode()


def _make_claude_conversation(
    messages: list[dict[str, Any]],
    *,
    uuid: str = "conv-001",
    name: str = "Test Conversation",
    created_at: str = "2024-01-15T10:00:00Z",
) -> dict[str, Any]:
    """Build a single Claude conversation dict."""
    return {
        "uuid": uuid,
        "name": name,
        "created_at": created_at,
        "messages": messages,
    }


class TestClaudeIngestorIntegration:
    """Tests for ChatbotFilter integration with ClaudeIngestor."""

    def test_filter_removes_system_prompts_during_parse(self) -> None:
        """ClaudeIngestor with filter should remove system prompts."""
        from creek.ingest.base import RawDocument, normalize_encoding
        from creek.ingest.claude import ClaudeIngestor

        chatbot_filter = ChatbotFilter()
        ingestor = ClaudeIngestor(chatbot_filter=chatbot_filter)

        conv = _make_claude_conversation(
            [
                {"role": "system", "content": "You are a helpful assistant."},
                {
                    "role": "human",
                    "content": "Tell me about quantum computing in detail.",
                    "created_at": "2024-01-15T10:01:00Z",
                },
                {
                    "role": "assistant",
                    "content": "Quantum computing is a type of computation.",
                    "created_at": "2024-01-15T10:02:00Z",
                },
            ]
        )
        export_bytes = _make_claude_export([conv])
        _, encoding = normalize_encoding(export_bytes)
        raw = RawDocument(
            path="fake/claude.json",
            content=export_bytes,
            metadata={},
            detected_encoding=encoding,
        )
        fragments = ingestor.parse(raw)
        # FEAT #553: one turn now splits into a human and an AI fragment.
        assert len(fragments) == 2
        assert "quantum" in fragments[0].content.lower()

    def test_filter_collapses_regenerations_during_parse(self) -> None:
        """ClaudeIngestor with filter should collapse regenerated responses."""
        from creek.ingest.base import RawDocument, normalize_encoding
        from creek.ingest.claude import ClaudeIngestor

        chatbot_filter = ChatbotFilter()
        ingestor = ClaudeIngestor(chatbot_filter=chatbot_filter)

        conv = _make_claude_conversation(
            [
                {
                    "role": "human",
                    "content": "Write me a poem about the ocean waves.",
                    "created_at": "2024-01-15T10:01:00Z",
                },
                {
                    "role": "assistant",
                    "content": "First draft of poem...",
                    "created_at": "2024-01-15T10:02:00Z",
                },
                {
                    "role": "assistant",
                    "content": "Final polished poem about ocean waves.",
                    "created_at": "2024-01-15T10:03:00Z",
                },
            ]
        )
        export_bytes = _make_claude_export([conv])
        _, encoding = normalize_encoding(export_bytes)
        raw = RawDocument(
            path="fake/claude.json",
            content=export_bytes,
            metadata={},
            detected_encoding=encoding,
        )
        fragments = ingestor.parse(raw)
        # FEAT #553: the collapsed assistant turn is now its own AI fragment.
        assert len(fragments) == 2
        assert "Final polished" in fragments[1].content

    def test_no_filter_keeps_all_messages(self) -> None:
        """ClaudeIngestor without filter should keep all non-system messages."""
        from creek.ingest.base import RawDocument, normalize_encoding
        from creek.ingest.claude import ClaudeIngestor

        ingestor = ClaudeIngestor()
        conv = _make_claude_conversation(
            [
                {"role": "system", "content": "You are helpful."},
                {
                    "role": "human",
                    "content": "Hi",
                    "created_at": "2024-01-15T10:01:00Z",
                },
                {
                    "role": "assistant",
                    "content": "Hello!",
                    "created_at": "2024-01-15T10:02:00Z",
                },
            ]
        )
        export_bytes = _make_claude_export([conv])
        _, encoding = normalize_encoding(export_bytes)
        raw = RawDocument(
            path="fake/claude.json",
            content=export_bytes,
            metadata={},
            detected_encoding=encoding,
        )
        fragments = ingestor.parse(raw)
        # Without filter, system is still skipped by _pair_turns but the short
        # human turn is kept — and FEAT #553 splits it into human + AI.
        assert len(fragments) == 2


# ---------------------------------------------------------------------------
# Integration — ChatGPTIngestor with ChatbotFilter
# ---------------------------------------------------------------------------


def _make_chatgpt_export(conversations: list[dict[str, Any]]) -> bytes:
    """Build a ChatGPT-format JSON export as bytes."""
    return json.dumps(conversations).encode()


def _make_chatgpt_mapping(
    messages: list[tuple[str, str]],
) -> dict[str, Any]:
    """Build a ChatGPT tree mapping from (role, content) pairs.

    Creates a simple linear tree (no branching).
    """
    mapping: dict[str, Any] = {}
    prev_id: str | None = None

    # Root node
    root_id = "node-root"
    mapping[root_id] = {
        "id": root_id,
        "parent": None,
        "children": [],
        "message": None,
    }
    prev_id = root_id

    for i, (role, content) in enumerate(messages):
        node_id = f"node-{i}"
        mapping[node_id] = {
            "id": node_id,
            "parent": prev_id,
            "children": [],
            "message": {
                "author": {"role": role},
                "content": {"parts": [content]},
                "create_time": 1705312860.0 + i,
            },
        }
        mapping[prev_id]["children"].append(node_id)
        prev_id = node_id

    return mapping


class TestChatGPTIngestorIntegration:
    """Tests for ChatbotFilter integration with ChatGPTIngestor."""

    def test_filter_removes_system_messages_during_parse(self) -> None:
        """ChatGPTIngestor with filter should remove system messages."""
        from creek.ingest.base import RawDocument, normalize_encoding
        from creek.ingest.chatgpt import ChatGPTIngestor

        chatbot_filter = ChatbotFilter()
        ingestor = ChatGPTIngestor(chatbot_filter=chatbot_filter)

        mapping = _make_chatgpt_mapping(
            [
                ("system", "You are a helpful AI assistant."),
                ("user", "What is machine learning and how does it work?"),
                ("assistant", "Machine learning is a subset of AI."),
            ]
        )
        conv = {
            "title": "ML Discussion",
            "create_time": 1705312860.0,
            "id": "conv-gpt-001",
            "mapping": mapping,
        }
        export_bytes = _make_chatgpt_export([conv])
        _, encoding = normalize_encoding(export_bytes)
        raw = RawDocument(
            path="fake/chatgpt.json",
            content=export_bytes,
            metadata={"source_type": "chatgpt"},
            detected_encoding=encoding,
        )
        fragments = ingestor.parse(raw)
        # FEAT #553: one turn now splits into a human and an AI fragment.
        assert len(fragments) == 2
        assert "machine learning" in fragments[0].content.lower()

    def test_filter_skips_tool_outputs_during_parse(self) -> None:
        """ChatGPTIngestor with filter should skip tool output messages."""
        from creek.ingest.base import RawDocument, normalize_encoding
        from creek.ingest.chatgpt import ChatGPTIngestor

        chatbot_filter = ChatbotFilter()
        ingestor = ChatGPTIngestor(chatbot_filter=chatbot_filter)

        mapping = _make_chatgpt_mapping(
            [
                ("user", "Search for the latest Python release information."),
                ("assistant", "Let me search for that information."),
                ("tool", "Python 3.12 was released October 2023."),
                ("assistant", "Python 3.12 was released in October 2023."),
            ]
        )
        conv = {
            "title": "Python Search",
            "create_time": 1705312860.0,
            "id": "conv-gpt-002",
            "mapping": mapping,
        }
        export_bytes = _make_chatgpt_export([conv])
        _, encoding = normalize_encoding(export_bytes)
        raw = RawDocument(
            path="fake/chatgpt.json",
            content=export_bytes,
            metadata={"source_type": "chatgpt"},
            detected_encoding=encoding,
        )
        fragments = ingestor.parse(raw)
        # Tool message filtered; user pairs with first assistant after filter
        assert len(fragments) >= 1
        for frag in fragments:
            assert "tool" not in frag.content.lower().split(":")[0]
