"""Decision support layer for the Creek system.

Provides decision detection in fragments, draft decision note generation,
and context gathering for the five-phase decision lifecycle.
"""

from creek.decision.detector import DecisionDetector, DetectedDecision

__all__ = ["DecisionDetector", "DetectedDecision"]
