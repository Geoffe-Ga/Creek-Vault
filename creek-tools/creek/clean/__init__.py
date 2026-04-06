"""Creek content cleaning module — quality scoring, deduplication, validation.

Provides the :class:`QualityScorer` for evaluating content quality using
entropy, stop-word ratio, length, and content-type heuristics, the
:class:`QualityResult` model for structured scoring output, the
:class:`Deduplicator` for detecting exact and content-hash-based duplicate
fragments, the :class:`FragmentValidator` for post-ingestion field,
encoding, and timestamp validation, the :class:`ContextExtractor`
for handling non-user content, and the :class:`ChatbotFilter` for
pre-ingestion chatbot noise removal.
"""

from creek.clean.context import ContextExtractor, ContextResult
from creek.clean.dedup import DeduplicationResult, Deduplicator
from creek.clean.filters.chatbot import (
    ChatbotFilter,
    ChatbotFilterConfig,
    ConversationFilterResult,
    MessageVerdict,
)
from creek.clean.quality import QualityResult, QualityScorer
from creek.clean.validator import FragmentValidator, ValidationResult, Violation

__all__ = [
    "ChatbotFilter",
    "ChatbotFilterConfig",
    "ContextExtractor",
    "ContextResult",
    "ConversationFilterResult",
    "DeduplicationResult",
    "Deduplicator",
    "FragmentValidator",
    "MessageVerdict",
    "QualityResult",
    "QualityScorer",
    "ValidationResult",
    "Violation",
]
