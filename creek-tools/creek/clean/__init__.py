"""Creek content cleaning module — quality scoring, deduplication, and filtering.

Provides the :class:`QualityScorer` for evaluating content quality using
entropy, stop-word ratio, length, and content-type heuristics, the
:class:`QualityResult` model for structured scoring output, the
:class:`Deduplicator` for detecting exact and content-hash-based duplicate
fragments, and the :class:`MarkdownFilter` for pre-ingestion markdown
filtering (empty/stub detection, template residue, broken wiki-links).
"""

from creek.clean.dedup import DeduplicationResult, Deduplicator
from creek.clean.markdown_filter import MarkdownFilter, MarkdownFilterResult
from creek.clean.quality import QualityResult, QualityScorer

__all__ = [
    "DeduplicationResult",
    "Deduplicator",
    "MarkdownFilter",
    "MarkdownFilterResult",
    "QualityResult",
    "QualityScorer",
]
