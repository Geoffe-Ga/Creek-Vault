"""Creek content cleaning module — quality scoring, deduplication, validation.

Provides the :class:`QualityScorer` for evaluating content quality using
entropy, stop-word ratio, length, and content-type heuristics, the
:class:`QualityResult` model for structured scoring output, the
:class:`Deduplicator` for detecting exact and content-hash-based duplicate
fragments, and the :class:`FragmentValidator` for post-ingestion field,
encoding, and timestamp validation.
"""

from creek.clean.dedup import DeduplicationResult, Deduplicator
from creek.clean.quality import QualityResult, QualityScorer
from creek.clean.validator import FragmentValidator, ValidationResult, Violation

__all__ = [
    "DeduplicationResult",
    "Deduplicator",
    "FragmentValidator",
    "QualityResult",
    "QualityScorer",
    "ValidationResult",
    "Violation",
]
