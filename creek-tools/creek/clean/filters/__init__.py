"""Creek content filters — pre-ingestion noise filtering.

Provides the :class:`FilterResult` model for structured filter decisions
and the :class:`DiscordFilter` for skipping bot messages, emoji-only
content, command invocations, media-only posts, short messages, and
link dumps from Discord data exports.
"""

from __future__ import annotations

from pydantic import BaseModel


class FilterResult(BaseModel):
    """Result of applying a content filter to a message.

    Attributes:
        keep: Whether the message should be kept (``True``) or
            filtered out (``False``).
        reason: Human-readable explanation of why the message was
            filtered or flagged.  ``None`` when the message passes
            all filters cleanly.
    """

    keep: bool
    reason: str | None = None


__all__ = [
    "FilterResult",
]
