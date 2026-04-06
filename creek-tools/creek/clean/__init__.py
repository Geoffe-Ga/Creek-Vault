"""Creek content cleaning module — quality scoring, deduplication, and filtering.

Provides the :class:`QualityScorer` for evaluating content quality using
entropy, stop-word ratio, length, and content-type heuristics, the
:class:`QualityResult` model for structured scoring output, the
:class:`Deduplicator` for detecting exact and content-hash-based duplicate
fragments, the :class:`ContextExtractor` for handling non-user content,
and the :class:`GoogleDriveFilter` for pre-ingestion filtering of Google
Drive staged files.
"""

from creek.clean.context import ContextExtractor, ContextResult
from creek.clean.dedup import DeduplicationResult, Deduplicator
from creek.clean.filters.google_drive import (
    GoogleDriveFilter,
    GoogleDriveFilterResult,
    StagedFile,
)
from creek.clean.quality import QualityResult, QualityScorer

__all__ = [
    "ContextExtractor",
    "ContextResult",
    "DeduplicationResult",
    "Deduplicator",
    "GoogleDriveFilter",
    "GoogleDriveFilterResult",
    "QualityResult",
    "QualityScorer",
    "StagedFile",
]
