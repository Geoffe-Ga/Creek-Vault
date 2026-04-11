"""Creek content cleaning — quality, deduplication, authorship, validation, hygiene.

Provides the :class:`QualityScorer` for evaluating content quality using
entropy, stop-word ratio, length, and content-type heuristics, the
:class:`QualityResult` model for structured scoring output, the
:class:`Deduplicator` for detecting exact and content-hash-based duplicate
fragments, the :class:`SemanticDeduplicator` for embedding-based
near-duplicate detection across sources, the :class:`AuthorshipTagger`
for classifying fragment authorship as self, ai, other, or collaborative,
the :class:`FragmentValidator` for post-ingestion field, encoding, and
timestamp validation, the :class:`ContextExtractor` for handling
non-user content, the :class:`ChatbotFilter` for pre-ingestion chatbot
noise removal, the :class:`GoogleDriveFilter` for pre-ingestion filtering
of Google Drive staged files, and vault hygiene scanners for orphans,
stale reviews, broken links, duplicates, and health reports.
"""

from creek.clean.authorship import AuthorshipResult, AuthorshipTagger
from creek.clean.context import ContextExtractor, ContextResult
from creek.clean.dedup import DeduplicationResult, Deduplicator
from creek.clean.filters.chatbot import (
    ChatbotFilter,
    ChatbotFilterConfig,
    ConversationFilterResult,
    MessageVerdict,
)
from creek.clean.filters.google_drive import (
    GoogleDriveFilter,
    GoogleDriveFilterResult,
    StagedFile,
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
from creek.clean.semantic_dedup import (
    SemanticDeduplicator,
    SemanticDuplicatePair,
    SemanticDuplicateResult,
)
from creek.clean.validator import FragmentValidator, ValidationResult, Violation

__all__ = [
    "AuthorshipResult",
    "AuthorshipTagger",
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
    "GoogleDriveFilter",
    "GoogleDriveFilterResult",
    "HygieneReport",
    "HygieneReporter",
    "MessageVerdict",
    "OrphanResult",
    "OrphanScanner",
    "QualityResult",
    "QualityScorer",
    "SemanticDeduplicator",
    "SemanticDuplicatePair",
    "SemanticDuplicateResult",
    "StagedFile",
    "StaleReviewResult",
    "StaleReviewScanner",
    "ValidationResult",
    "Violation",
]
