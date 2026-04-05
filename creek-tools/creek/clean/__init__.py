"""Creek content cleaning module — quality scoring, deduplication, and filtering.

Provides the :class:`QualityScorer` for evaluating content quality using
entropy, stop-word ratio, length, and content-type heuristics, the
:class:`QualityResult` model for structured scoring output, the
:class:`Deduplicator` for detecting exact and content-hash-based duplicate
fragments, and the :class:`SemanticDeduplicator` for embedding-based
near-duplicate detection across sources.
"""

from creek.clean.dedup import DeduplicationResult, Deduplicator
from creek.clean.quality import QualityResult, QualityScorer
from creek.clean.semantic_dedup import (
    SemanticDeduplicator,
    SemanticDuplicatePair,
    SemanticDuplicateResult,
)

__all__ = [
    "DeduplicationResult",
    "Deduplicator",
    "QualityResult",
    "QualityScorer",
    "SemanticDeduplicator",
    "SemanticDuplicatePair",
    "SemanticDuplicateResult",
]
