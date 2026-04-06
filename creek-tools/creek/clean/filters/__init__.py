"""Pre-ingestion content filters for the Creek clean pipeline.

Provides :class:`FilterResult`, the common return type for all filters,
and re-exports concrete filter implementations: :class:`DiscordFilter`
for Discord messages, :class:`GoogleDriveFilter` for Google Drive staged
files, and :class:`MarkdownFilter` for markdown files.
"""

from creek.clean.filters._result import FilterResult
from creek.clean.filters.discord import (
    DiscordFilter,
    DiscordFilterConfig,
    FilterStats,
)
from creek.clean.filters.google_drive import (
    GoogleDriveFilter,
    GoogleDriveFilterResult,
    StagedFile,
)
from creek.clean.filters.markdown import MarkdownFilter

__all__ = [
    "DiscordFilter",
    "DiscordFilterConfig",
    "FilterResult",
    "FilterStats",
    "GoogleDriveFilter",
    "GoogleDriveFilterResult",
    "MarkdownFilter",
    "StagedFile",
]
