"""Pre-ingestion content filters for the Creek clean pipeline.

Provides :class:`FilterResult`, the common return type for all filters,
and re-exports concrete filter implementations: :class:`ChatbotFilter`
for chatbot noise removal, :class:`DiscordFilter` for Discord messages,
:class:`GoogleDriveFilter` for Google Drive staged files, and
:class:`MarkdownFilter` for markdown file filtering.
"""

from creek.clean.filters._result import FilterResult
from creek.clean.filters.chatbot import (
    ChatbotFilter,
    ChatbotFilterConfig,
    ConversationFilterResult,
    MessageVerdict,
)
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
    "ChatbotFilter",
    "ChatbotFilterConfig",
    "ConversationFilterResult",
    "DiscordFilter",
    "DiscordFilterConfig",
    "FilterResult",
    "FilterStats",
    "GoogleDriveFilter",
    "GoogleDriveFilterResult",
    "MarkdownFilter",
    "MessageVerdict",
    "StagedFile",
]
