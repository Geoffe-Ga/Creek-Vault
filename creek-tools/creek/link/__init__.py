"""Creek linking pipeline — embedding, temporal, thread, and eddy linkers.

This package provides the four linking stages of the Creek pipeline.

Public API:
    - ``EmbeddingLinker`` — generate embeddings and find semantic resonances
    - ``EmbeddingModelUnavailableError`` — typed failure when the
      sentence-transformer model cannot be loaded
    - ``Resonance`` — hierarchy-aware resonance edge (FEAT-024)
    - ``TemporalLink`` — scored temporal proximity link between two fragments
    - ``TemporalLinker`` — find temporal proximity links across sources
    - ``ThreadDetector`` — detect narrative threads
    - ``EddyDetector`` — detect topic cluster eddies

Orchestration lives in :mod:`creek.link.link_engine` — one entry point
(``run_link``) shared by ``creek link``, ``creek fill``, ``creek sync``
and ``creek process``. The former ``LinkingPipeline`` was a second,
count-only orchestrator that persisted nothing; it was deleted in #1303
rather than taught to write, so there is exactly one linking code path.
"""

from creek.link.eddies import EddyDetector
from creek.link.embeddings import (
    EmbeddingLinker,
    EmbeddingModelUnavailableError,
    Resonance,
)
from creek.link.temporal import TemporalLink, TemporalLinker
from creek.link.threads import ThreadDetector

__all__ = [
    "EddyDetector",
    "EmbeddingLinker",
    "EmbeddingModelUnavailableError",
    "Resonance",
    "TemporalLink",
    "TemporalLinker",
    "ThreadDetector",
]
