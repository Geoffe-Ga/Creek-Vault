"""Creek linking pipeline — embedding, temporal, thread, and eddy linkers.

This package provides the four linking stages of the Creek pipeline.

Public API:
    - ``EmbeddingLinker`` — generate embeddings and find semantic resonances
    - ``Resonance`` — hierarchy-aware resonance edge (FEAT-024)
    - ``TemporalLink`` — scored temporal proximity link between two fragments
    - ``TemporalLinker`` — find temporal proximity links across sources
    - ``ThreadDetector`` — detect narrative threads
    - ``EddyDetector`` — detect topic cluster eddies
    - ``LinkingResult`` — Pydantic model for pipeline result counts
    - ``LinkingPipeline`` — orchestrate all four linking stages
"""

from creek.link.eddies import EddyDetector
from creek.link.embeddings import EmbeddingLinker, Resonance
from creek.link.linker import LinkingPipeline, LinkingResult
from creek.link.temporal import TemporalLink, TemporalLinker
from creek.link.threads import ThreadDetector

__all__ = [
    "EddyDetector",
    "EmbeddingLinker",
    "LinkingPipeline",
    "LinkingResult",
    "Resonance",
    "TemporalLink",
    "TemporalLinker",
    "ThreadDetector",
]
