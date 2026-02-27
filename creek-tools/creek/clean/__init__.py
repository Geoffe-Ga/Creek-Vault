"""Creek content cleaning module — quality scoring and content filtering.

Provides the :class:`QualityScorer` for evaluating content quality using
entropy, stop-word ratio, length, and content-type heuristics, and the
:class:`QualityResult` model for structured scoring output.
"""

from creek.clean.quality import QualityResult, QualityScorer

__all__ = [
    "QualityResult",
    "QualityScorer",
]
