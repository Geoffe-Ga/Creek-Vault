"""Creek generate package — index and note generation for the Creek vault."""

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
from creek.generate.tags import TagGardenGenerator, TagScanResult

__all__ = [
    "CONTRADICTION_KEYWORDS",
    "DECISION_KEYWORDS",
    "FREQUENCY_COLORS",
    "FREQUENCY_NAMES",
    "PRACTICE_MAP",
    "REFLECTION_PROMPT",
    "DecisionContext",
    "DecisionContextGatherer",
    "DecisionDetector",
    "IndexGenerator",
    "Paradox",
    "ParadoxDetector",
    "TagGardenGenerator",
    "TagScanResult",
    "decision_from_note",
]
