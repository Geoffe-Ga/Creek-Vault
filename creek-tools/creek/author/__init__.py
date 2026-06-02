"""Creek Writing Desk — multi-agent authoring package (FEAT-041).

This package wires the author desk end-to-end with typed stubs (#455): a
:class:`~creek.author.conductor.Conductor` drives stub specialist agents, a
stub voice agent, and a stub reflection node to return a shaped
:class:`~creek.author.models.AuthoredDraft`. Later FEAT-041 issues replace the
stub bodies with real retrieval, synthesis, voicing, and judging behind the
same seams.
"""

from __future__ import annotations

from creek.author.client import AuthorLLMClient
from creek.author.conductor import (
    SUPPORTED_MEDIUMS,
    Conductor,
    build_default_conductor,
    run_author,
)
from creek.author.models import (
    AuthoredDraft,
    EvidenceBundle,
    EvidenceClaim,
    Medium,
    ReflectionVerdict,
)

__all__ = [
    "SUPPORTED_MEDIUMS",
    "AuthorLLMClient",
    "AuthoredDraft",
    "Conductor",
    "EvidenceBundle",
    "EvidenceClaim",
    "Medium",
    "ReflectionVerdict",
    "build_default_conductor",
    "run_author",
]
