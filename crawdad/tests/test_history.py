"""Tests for ``crawdad.history``."""

from __future__ import annotations

import pytest

from crawdad.config import HISTORY_MAX_CHARS_PER_ENTRY, HISTORY_MAX_ENTRIES
from crawdad.history import ConversationHistory, HistoryEntry


def test_history_starts_empty() -> None:
    """A fresh history has no entries."""
    history = ConversationHistory()

    assert history.as_list() == []


def test_history_append_records_role_and_content() -> None:
    """Entries land on the deque in document order."""
    history = ConversationHistory()

    history.append("user", "hello")
    history.append("assistant", "world")

    assert history.as_list() == [
        HistoryEntry(role="user", content="hello"),
        HistoryEntry(role="assistant", content="world"),
    ]


def test_history_truncates_long_content() -> None:
    """Per-entry content is capped at HISTORY_MAX_CHARS_PER_ENTRY chars."""
    history = ConversationHistory()
    long_message = "x" * (HISTORY_MAX_CHARS_PER_ENTRY + 500)

    history.append("user", long_message)

    stored = history.as_list()[0].content
    assert len(stored) == HISTORY_MAX_CHARS_PER_ENTRY
    assert stored == "x" * HISTORY_MAX_CHARS_PER_ENTRY


def test_history_evicts_oldest_beyond_max_entries() -> None:
    """The 21st append drops the first entry — strict FIFO at the cap."""
    history = ConversationHistory()
    for index in range(HISTORY_MAX_ENTRIES + 1):
        history.append("user", f"msg-{index}")

    entries = history.as_list()
    assert len(entries) == HISTORY_MAX_ENTRIES
    # First message (msg-0) was evicted; second (msg-1) is now oldest.
    assert entries[0].content == "msg-1"
    assert entries[-1].content == f"msg-{HISTORY_MAX_ENTRIES}"


def test_history_clear_resets() -> None:
    """``clear()`` empties the deque for session resets."""
    history = ConversationHistory()
    history.append("user", "hello")
    history.append("assistant", "world")

    history.clear()

    assert history.as_list() == []


def test_history_rejects_empty_role() -> None:
    """An empty role is a programming error, caught at append time."""
    history = ConversationHistory()

    with pytest.raises(ValueError, match="role"):
        history.append("", "anything")


def test_history_entry_is_frozen() -> None:
    """HistoryEntry instances are immutable so prompt assembly can't mutate."""
    entry = HistoryEntry(role="user", content="hello")

    with pytest.raises((TypeError, AttributeError, ValueError)):
        entry.role = "assistant"  # type: ignore[misc]
