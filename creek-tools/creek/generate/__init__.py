"""Creek generate package — index and note generation for the Creek vault."""

from creek.generate.compost import (
    ABANDONMENT_KEYWORDS,
    CompostCandidate,
    CompostTracker,
)
from creek.generate.decisions import (
    DECISION_KEYWORDS,
    PRACTICE_MAP,
    DecisionContext,
    DecisionContextGatherer,
    DecisionDetector,
    decision_from_note,
)
from creek.generate.indexes import FREQUENCY_COLORS, FREQUENCY_NAMES, IndexGenerator
from creek.generate.paradox import (
    CONTRADICTION_KEYWORDS,
    REFLECTION_PROMPT,
    Paradox,
    ParadoxDetector,
)
from creek.generate.synchronicity import (
    DEFAULT_MIN_TIME_GAP_DAYS,
    DEFAULT_SIMILARITY_THRESHOLD,
    SynchronicityDetector,
)
from creek.generate.tags import TagGardenGenerator, TagScanResult

__all__ = [
    "ABANDONMENT_KEYWORDS",
    "CONTRADICTION_KEYWORDS",
    "DECISION_KEYWORDS",
    "DEFAULT_MIN_TIME_GAP_DAYS",
    "DEFAULT_SIMILARITY_THRESHOLD",
    "FREQUENCY_COLORS",
    "FREQUENCY_NAMES",
    "PRACTICE_MAP",
    "REFLECTION_PROMPT",
    "CompostCandidate",
    "CompostTracker",
    "DecisionContext",
    "DecisionContextGatherer",
    "DecisionDetector",
    "IndexGenerator",
    "Paradox",
    "ParadoxDetector",
    "SynchronicityDetector",
    "TagGardenGenerator",
    "TagScanResult",
    "decision_from_note",
]
