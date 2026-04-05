"""Creek content cleaning — quality, deduplication, authorship, and filtering.

Provides the :class:`QualityScorer` for evaluating content quality using
entropy, stop-word ratio, length, and content-type heuristics, the
:class:`QualityResult` model for structured scoring output, the
:class:`Deduplicator` for detecting exact and content-hash-based duplicate
fragments, and the :class:`AuthorshipTagger` for classifying fragment
authorship as self, ai, other, or collaborative.
"""

from creek.clean.authorship import AuthorshipResult, AuthorshipTagger
from creek.clean.dedup import DeduplicationResult, Deduplicator
from creek.clean.quality import QualityResult, QualityScorer

__all__ = [
    "AuthorshipResult",
    "AuthorshipTagger",
    "DeduplicationResult",
    "Deduplicator",
    "QualityResult",
    "QualityScorer",
]
