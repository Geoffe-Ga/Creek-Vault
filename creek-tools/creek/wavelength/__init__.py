"""Creek wavelength tracking — phase detection, dosage trends, and observations.

Provides the :class:`WavelengthTracker` for analyzing classified fragments
over time windows to detect dominant phases, phase transitions, and
medicine/toxic dosage trends.  Also provides structured result models:
:class:`WindowSummary`, :class:`PhaseTransition`, and :class:`DosageTrend`.
"""

from creek.wavelength.tracker import (
    DosageTrend,
    PhaseTransition,
    WavelengthTracker,
    WindowSummary,
)

__all__ = [
    "DosageTrend",
    "PhaseTransition",
    "WavelengthTracker",
    "WindowSummary",
]
