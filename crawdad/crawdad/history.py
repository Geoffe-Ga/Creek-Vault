"""Episodic-memory-lite for the CrawDad agent loop.

ADOPT-008 §truncation discipline: the last 20 conversation entries,
each truncated to 2000 characters. A hard cliff — no smarter
compression in v1.0 (FEAT-016 may refine).

``ConversationHistory`` is intentionally a tiny class around
:class:`collections.deque`. It owns the eviction + truncation rules so
callers (router prompt builder, loop) never have to reason about the
budget.
"""

from __future__ import annotations

from collections import deque

from pydantic import BaseModel, ConfigDict

from crawdad.config import HISTORY_MAX_CHARS_PER_ENTRY, HISTORY_MAX_ENTRIES


class HistoryEntry(BaseModel):
    """A single turn in the conversation transcript."""

    model_config = ConfigDict(frozen=True)

    role: str
    content: str


class ConversationHistory:
    """Bounded FIFO transcript for the current Discord session.

    The bound is enforced on every :meth:`append`; the
    :meth:`as_list` view never reflects more than
    :data:`crawdad.config.HISTORY_MAX_ENTRIES` items.
    """

    def __init__(self) -> None:
        """Start an empty transcript."""
        self._entries: deque[HistoryEntry] = deque(maxlen=HISTORY_MAX_ENTRIES)

    def append(self, role: str, content: str) -> None:
        """Record one turn; truncate ``content`` to the per-entry budget.

        Args:
            role: ``"user"`` or ``"assistant"`` (free-form so future
                tool-use turns can label themselves; not enforced).
            content: The text of the turn. Truncated to
                :data:`crawdad.config.HISTORY_MAX_CHARS_PER_ENTRY` chars.

        Raises:
            ValueError: when ``role`` is the empty string — almost
                certainly a programming error.
        """
        if not role:
            msg = "role must be a non-empty string"
            raise ValueError(msg)
        truncated = content[:HISTORY_MAX_CHARS_PER_ENTRY]
        self._entries.append(HistoryEntry(role=role, content=truncated))

    def as_list(self) -> list[HistoryEntry]:
        """Return a list snapshot suitable for prompt assembly."""
        return list(self._entries)

    def clear(self) -> None:
        """Drop every entry; used by 5-round cap reset (FEAT-015)."""
        self._entries.clear()
