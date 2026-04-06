"""Creek content cleaning — quality scoring, deduplication, validation, hygiene.

Provides the :class:`QualityScorer` for evaluating content quality using
entropy, stop-word ratio, length, and content-type heuristics, the
:class:`QualityResult` model for structured scoring output, the
:class:`Deduplicator` for detecting exact and content-hash-based duplicate
fragments, the :class:`FragmentValidator` for post-ingestion field,
encoding, and timestamp validation, the :class:`ContextExtractor`
for handling non-user content, the :class:`ChatbotFilter` for
pre-ingestion chatbot noise removal, and vault hygiene scanners for
orphans, stale reviews, broken links, duplicates, and health reports.
"""

from creek.clean.context import ContextExtractor, ContextResult
from creek.clean.dedup import DeduplicationResult, Deduplicator
from creek.clean.filters.chatbot import (
    ChatbotFilter,
    ChatbotFilterConfig,
    ConversationFilterResult,
    MessageVerdict,
)
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
from creek.clean.validator import FragmentValidator, ValidationResult, Violation

__all__ = [
    "BrokenLinkResult",
    "BrokenLinkScanner",
    "ChatbotFilter",
    "ChatbotFilterConfig",
    "ContextExtractor",
    "ContextResult",
    "ConversationFilterResult",
    "DeduplicationResult",
    "Deduplicator",
    "DuplicateCandidate",
    "DuplicateResult",
    "DuplicateScanner",
    "FragmentValidator",
    "HygieneReport",
    "HygieneReporter",
    "MessageVerdict",
    "OrphanResult",
    "OrphanScanner",
    "QualityResult",
    "QualityScorer",
    "StaleReviewResult",
    "StaleReviewScanner",
    "ValidationResult",
    "Violation",
]
