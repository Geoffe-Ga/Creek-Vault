"""Creek linking pipeline — stubs for embedding, temporal, thread, and eddy linkers.

This package provides stub implementations for the four linking stages
of the Creek pipeline.  Real implementations will be added in issues #28-33.

Public API:
    - ``EmbeddingLinker`` — generate embeddings and find semantic resonances
    - ``TemporalLinker`` — find temporal proximity links
    - ``ThreadDetector`` — detect narrative threads
    - ``EddyDetector`` — detect topic cluster eddies
    - ``LinkingResult`` — Pydantic model for pipeline result counts
    - ``LinkingPipeline`` — orchestrate all four linking stages
"""

from creek.link.eddies import EddyDetector
from creek.link.embeddings import EmbeddingLinker
from creek.link.linker import LinkingPipeline, LinkingResult
from creek.link.temporal import TemporalLinker
from creek.link.threads import ThreadDetector

__all__ = [
    "EddyDetector",
    "EmbeddingLinker",
    "LinkingPipeline",
    "LinkingResult",
    "TemporalLinker",
    "ThreadDetector",
]
