"""Creek decision support layer — detect decisions and gather context.

This package implements the Decision Support Layer (Section 12 of the
Creek Ontology), providing decision detection from fragment content and
context gathering from related threads, past decisions, praxis notes,
and wavelength observations.

Public API:
    - ``DecisionDetector`` — scan fragments for decision-relevant content
    - ``ContextGatherer`` — gather related context for a decision
    - ``DecisionContext`` — Pydantic model for gathered context
    - ``DecisionResult`` — Pydantic model for pipeline result counts
    - ``DecisionPipeline`` — orchestrate detection and context gathering
"""

from creek.decide.context import ContextGatherer, DecisionContext
from creek.decide.detector import DecisionDetector
from creek.decide.pipeline import DecisionPipeline, DecisionResult

__all__ = [
    "ContextGatherer",
    "DecisionContext",
    "DecisionDetector",
    "DecisionPipeline",
    "DecisionResult",
]
