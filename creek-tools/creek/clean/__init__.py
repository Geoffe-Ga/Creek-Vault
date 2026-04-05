"""Creek content cleaning — quality scoring, deduplication, hygiene, filtering.

Provides the :class:`QualityScorer` for evaluating content quality using
entropy, stop-word ratio, length, and content-type heuristics, the
:class:`QualityResult` model for structured scoring output, the
:class:`Deduplicator` for detecting exact and content-hash-based duplicate
fragments, and vault hygiene scanners for orphans, stale reviews, broken
links, duplicates, and health reports.
"""

from creek.clean.dedup import DeduplicationResult, Deduplicator
from creek.clean.hygiene import (
    BrokenLinkResult,
    BrokenLinkScanner,
    DuplicateCandidate,
    DuplicateResult,
    DuplicateScanner,
    HygieneReport,
    HygieneReporter,
    OrphanResult,
    OrphanScanner,
    StaleReviewResult,
    StaleReviewScanner,
)
from creek.clean.quality import QualityResult, QualityScorer

__all__ = [
    "BrokenLinkResult",
    "BrokenLinkScanner",
    "DeduplicationResult",
    "Deduplicator",
    "DuplicateCandidate",
    "DuplicateResult",
    "DuplicateScanner",
    "HygieneReport",
    "HygieneReporter",
    "OrphanResult",
    "OrphanScanner",
    "QualityResult",
    "QualityScorer",
    "StaleReviewResult",
    "StaleReviewScanner",
]
