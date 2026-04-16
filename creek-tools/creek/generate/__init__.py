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
from creek.generate.wavelength import (
    DEFAULT_ROLLING_WEEKS,
    DEFAULT_TOXIC_CONSECUTIVE_WEEKS,
    DEFAULT_TOXIC_THRESHOLD,
    DEFAULT_WINDOW_DAYS,
    DOMAIN_MAPPINGS,
    NOTABLE_FRAGMENT_LIMIT,
    PHASE_DESCRIPTIONS,
    PROGRESSION_CHART_MAX_WIDTH,
    DosageTrend,
    PhaseTransition,
    WavelengthSnapshot,
    WavelengthTracker,
)

__all__ = [
    "ABANDONMENT_KEYWORDS",
    "CONTRADICTION_KEYWORDS",
    "DECISION_KEYWORDS",
    "DEFAULT_MIN_TIME_GAP_DAYS",
    "DEFAULT_ROLLING_WEEKS",
    "DEFAULT_SIMILARITY_THRESHOLD",
    "DEFAULT_TOXIC_CONSECUTIVE_WEEKS",
    "DEFAULT_TOXIC_THRESHOLD",
    "DEFAULT_WINDOW_DAYS",
    "DOMAIN_MAPPINGS",
    "FREQUENCY_COLORS",
    "FREQUENCY_NAMES",
    "NOTABLE_FRAGMENT_LIMIT",
    "PHASE_DESCRIPTIONS",
    "PRACTICE_MAP",
    "PROGRESSION_CHART_MAX_WIDTH",
    "REFLECTION_PROMPT",
    "CompostCandidate",
    "CompostTracker",
    "DecisionContext",
    "DecisionContextGatherer",
    "DecisionDetector",
    "DosageTrend",
    "IndexGenerator",
    "Paradox",
    "ParadoxDetector",
    "PhaseTransition",
    "SynchronicityDetector",
    "TagGardenGenerator",
    "TagScanResult",
    "WavelengthSnapshot",
    "WavelengthTracker",
    "decision_from_note",
]
