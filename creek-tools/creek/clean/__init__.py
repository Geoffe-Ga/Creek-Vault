"""Creek content cleaning module — quality scoring, deduplication, and filtering.

Provides the :class:`QualityScorer` for evaluating content quality using
entropy, stop-word ratio, length, and content-type heuristics, the
:class:`QualityResult` model for structured scoring output, the
:class:`Deduplicator` for detecting exact and content-hash-based duplicate
fragments, and the :class:`ChatbotFilter` for pre-ingestion chatbot noise
removal.
"""

from creek.clean.dedup import DeduplicationResult, Deduplicator
from creek.clean.filters.chatbot import (
    ChatbotFilter,
    ChatbotFilterConfig,
    ConversationFilterResult,
    MessageVerdict,
)
from creek.clean.quality import QualityResult, QualityScorer

__all__ = [
    "ChatbotFilter",
    "ChatbotFilterConfig",
    "ConversationFilterResult",
    "DeduplicationResult",
    "Deduplicator",
    "MessageVerdict",
    "QualityResult",
    "QualityScorer",
]
