"""Creek pre-ingestion filters for source-specific data cleaning.

Provides filters that operate on staged files *before* they enter the
main ingest pipeline. Each filter evaluates file-level metadata and
content to decide whether a file should be kept, skipped, or flagged
for review.
"""

from creek.clean.filters.google_drive import (
    GoogleDriveFilter,
    GoogleDriveFilterResult,
    StagedFile,
)

__all__ = [
    "GoogleDriveFilter",
    "GoogleDriveFilterResult",
    "StagedFile",
]
