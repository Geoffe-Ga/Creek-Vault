"""Creek Writing Desk — multi-agent authoring package (FEAT-041).

A :class:`~creek.author.conductor.Conductor` drives the desk end-to-end and
returns a shaped :class:`~creek.author.models.AuthoredDraft`. The specialist
agents (graph, retrieval, ontology) and the reflection node do **real,
deterministic** work — link-graph walks, embedding retrieval, rule-based
ontology grounding, and quality-gate judging (no LLM). The voice node renders
**live** through the router-resolved ``generation`` provider when one is
available (tier-routed so Intimate content stays local, #658/#661), falling back
to a deterministic rendering otherwise. The original #455 scaffold's stubs have
since been replaced behind the same seams.
"""

from __future__ import annotations

from creek.author.client import AuthorLLMClient, resolve_voice_model
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
    "resolve_voice_model",
    "run_author",
]
