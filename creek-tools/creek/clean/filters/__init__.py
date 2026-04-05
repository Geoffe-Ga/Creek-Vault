"""Discord-specific pre-ingestion filters.

Provides :class:`DiscordFilter` for skipping low-value Discord messages
(bots, emoji-only, command invocations, media-only, short messages,
link dumps) before fragment creation.
"""

from creek.clean.filters.discord import (
    DiscordFilter,
    DiscordFilterConfig,
    FilterResult,
    FilterStats,
)

__all__ = [
    "DiscordFilter",
    "DiscordFilterConfig",
    "FilterResult",
    "FilterStats",
]
