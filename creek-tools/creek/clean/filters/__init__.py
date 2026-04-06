"""Pre-ingestion content filters for the Creek clean pipeline.

Provides :class:`FilterResult`, the common return type for all filters,
and re-exports concrete filter implementations.

Exports:
    FilterResult: Pydantic model describing a filter decision.
    GoogleDriveFilter: Filter for Google Drive staged files.
    GoogleDriveFilterResult: Result model for GoogleDriveFilter.
    MarkdownFilter: Filter for markdown files before ingestion.
    StagedFile: Input model for Google Drive staged file metadata.
"""

from creek.clean.filters._result import FilterResult
from creek.clean.filters.google_drive import (
    GoogleDriveFilter,
    GoogleDriveFilterResult,
    StagedFile,
)
from creek.clean.filters.markdown import MarkdownFilter

__all__ = [
    "FilterResult",
    "GoogleDriveFilter",
    "GoogleDriveFilterResult",
    "MarkdownFilter",
    "StagedFile",
]
