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
    AuthorPlan,
    Conductor,
    build_default_conductor,
    plan_author,
    require_supported_medium,
    run_author,
)
from creek.author.contracts import (
    CHAT_MAX_CHARS,
    ContractConformanceError,
    assert_contract_conformant,
    load_medium_contract,
)
from creek.author.models import (
    AuthoredDraft,
    EvidenceBundle,
    EvidenceClaim,
    Medium,
    ReflectionFinding,
    ReflectionResult,
    ReflectionVerdict,
)

__all__ = [
    "CHAT_MAX_CHARS",
    "SUPPORTED_MEDIUMS",
    "AuthorLLMClient",
    "AuthorPlan",
    "AuthoredDraft",
    "Conductor",
    "ContractConformanceError",
    "EvidenceBundle",
    "EvidenceClaim",
    "Medium",
    "ReflectionFinding",
    "ReflectionResult",
    "ReflectionVerdict",
    "assert_contract_conformant",
    "build_default_conductor",
    "load_medium_contract",
    "plan_author",
    "require_supported_medium",
    "run_author",
]
