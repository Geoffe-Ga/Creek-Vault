"""The Conductor that orchestrates the Creek Writing Desk (FEAT-041, #455).

The conductor drives the end-to-end flow: each specialist gathers structured
evidence, the bundles are synthesized, the voice agent renders a draft, and the
reflection node judges it — looping on ``REVISE`` up to ``max_rounds``. A draft
still in ``REVISE`` once the round budget is exhausted is escalated to a human
(``ESCALATE``) rather than shipped (#473), then returned as a shaped
:class:`~creek.author.models.AuthoredDraft`. The reflection node is a real
deterministic judge; the remaining collaborators are typed stubs that later
issues swap behind these same seams.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Protocol, TypedDict, cast

from creek.author.agents import Specialist, default_specialists
from creek.author.contracts import CHAT_MAX_CHARS, load_medium_contract
from creek.author.models import (
    AuthoredDraft,
    EvidenceBundle,
    EvidenceClaim,
    Medium,
    ReflectionResult,
    ReflectionVerdict,
)
from creek.author.reflection import ReflectionNode
from creek.author.voice import VoiceAgent
from creek.compile.provenance import ProvenanceEntry

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from creek.author.client import AuthorLLMClient
    from creek.models import MediumContract

logger = logging.getLogger(__name__)

#: Mediums the conductor will run. ``research``/``chat``/``essay``/
#: ``research-piece``/``book-report``/``how-to`` are all wired — ``how-to``
#: completes the medium set.
SUPPORTED_MEDIUMS: frozenset[str] = frozenset(
    {"research", "chat", "essay", "research-piece", "book-report", "how-to"}
)

#: Fixed pipeline steps that follow the specialist roster, in order.
_DOWNSTREAM_STEPS: tuple[str, ...] = ("synthesize", "voice", "reflect")

#: Excerpt length kept on each provenance entry's ``claim_excerpt``.
_EXCERPT_LEN: int = 80


class VoiceRenderer(Protocol):
    """Anything that renders evidence into a draft body."""

    def render(
        self,
        query: str,
        evidence: EvidenceBundle,
        vault: Path | None = None,
        *,
        medium: Medium | None = None,
        contract: MediumContract | None = None,
    ) -> str:
        """Return draft prose for *query* from *evidence* (in the owner's voice)."""
        ...


class Reflector(Protocol):
    """Anything that judges a draft body against its evidence."""

    def review(
        self,
        body: str,
        evidence: EvidenceBundle,
        rubric: dict[str, float] | None = None,
        *,
        contract: MediumContract | None = None,
        vault: Path | None = None,
    ) -> ReflectionResult:
        """Return a structured result for *body* given *evidence* and *rubric*."""
        ...


def require_supported_medium(medium: str) -> Medium:
    """Validate *medium* and return it as a :data:`Medium` literal.

    Args:
        medium: The requested medium.

    Returns:
        The validated medium literal.

    Raises:
        ValueError: When *medium* is not wired in the skeleton.
    """
    if medium not in SUPPORTED_MEDIUMS:
        wired = ", ".join(repr(m) for m in sorted(SUPPORTED_MEDIUMS))
        msg = (
            f"Unsupported medium {medium!r}; only {wired} "
            "are wired in the Writing Desk (FEAT-041)."
        )
        raise ValueError(msg)
    # Echo back the validated medium rather than a hard-coded literal so that
    # adding a medium to SUPPORTED_MEDIUMS cannot silently mislabel a draft.
    return cast("Medium", medium)


def _claims_to_provenance(claims: Sequence[EvidenceClaim]) -> list[ProvenanceEntry]:
    """Render evidence *claims* into mock provenance entries.

    Args:
        claims: The claims to trace.

    Returns:
        One :class:`ProvenanceEntry` per claim, numbered ``claim-001`` up.
    """
    stamped = datetime.now(tz=UTC)
    return [
        ProvenanceEntry(
            claim_id=f"claim-{index:03d}",
            claim_excerpt=claim.claim[:_EXCERPT_LEN],
            fragment_ids=claim.source_fragments.copy(),
            compiled_at=stamped,
            compile_method="manual",
        )
        for index, claim in enumerate(claims, start=1)
    ]


class Conductor:
    """Orchestrates specialists, the voice agent, and the reflection node.

    Attributes:
        specialists: The ordered specialist roster.
        voice: The agent that renders evidence into a draft.
        reflection: The node that judges each drafted body.
        max_rounds: Upper bound on voice/reflect rounds.
        llm_client: Optional network seam; unused by the stub collaborators
            but injected here so issue #460 can wire real LLM calls.
        contract: The medium contract driving this run; its reflection rubric
            is exposed to the reflection node (FEAT-041 #459).
    """

    def __init__(
        self,
        specialists: Sequence[Specialist],
        voice: VoiceRenderer,
        reflection: Reflector,
        *,
        max_rounds: int,
        llm_client: AuthorLLMClient | None = None,
        contract: MediumContract | None = None,
    ) -> None:
        """Store collaborators, the round bound, and the medium contract."""
        self.specialists = list(specialists)
        self.voice = voice
        self.reflection = reflection
        self.max_rounds = max_rounds
        self.llm_client = llm_client
        self.contract = contract

    def plan(self) -> list[str]:
        """Return the ordered pipeline step names for display/dry-run."""
        return [s.name for s in self.specialists] + list(_DOWNSTREAM_STEPS)

    def gather_evidence(self, query: str, vault: Path) -> EvidenceBundle:
        """Run the specialist roster then the synthesize step.

        Args:
            query: The user query.
            vault: The vault the specialists read from.

        Returns:
            The synthesized :class:`EvidenceBundle`.
        """
        bundles = [specialist.gather(query, vault) for specialist in self.specialists]
        return self.synthesize(bundles)

    def synthesize(self, bundles: Sequence[EvidenceBundle]) -> EvidenceBundle:
        """Merge per-specialist *bundles* into one GROUNDED bundle.

        Every retained claim must trace to at least one source fragment: a
        claim with empty ``source_fragments`` is dropped (and logged), never
        fabricated. The retained claims keep their stable specialist/insertion
        order; when ``self.contract`` is present that order is the documented
        assembly order honouring the contract's ``structure`` (the load-bearing
        invariant is grounding, not per-section placement). The first non-None
        ontology any specialist produced is carried through.

        Args:
            bundles: One evidence bundle per specialist.

        Returns:
            A single :class:`EvidenceBundle` of grounded claims plus the first
            ontological analysis any specialist produced.
        """
        claims: list[EvidenceClaim] = []
        ontology = None
        for bundle in bundles:
            claims.extend(self._grounded_claims(bundle))
            if ontology is None and bundle.ontology is not None:
                ontology = bundle.ontology
        return EvidenceBundle(claims=claims, ontology=ontology)

    @staticmethod
    def _grounded_claims(bundle: EvidenceBundle) -> list[EvidenceClaim]:
        """Return *bundle*'s claims that trace to a fragment, logging drops.

        A claim with no ``source_fragments`` cannot be grounded, so it is
        dropped here rather than passed to the voice agent where it could be
        presented as fact without provenance.
        """
        kept: list[EvidenceClaim] = []
        for claim in bundle.claims:
            if claim.source_fragments:
                kept.append(claim)
            else:
                logger.warning(
                    "Dropping ungrounded claim (no source fragments): %r",
                    claim.claim,
                )
        return kept

    def run(self, *, medium: str, query: str, vault: Path) -> AuthoredDraft:
        """Run the full author pipeline and return a shaped draft.

        Args:
            medium: The requested medium (only ``research`` is wired).
            query: The user query.
            vault: The vault to author from.

        Returns:
            The :class:`AuthoredDraft` with mock provenance and a verdict. A
            draft that never cleared ``REVISE`` within ``max_rounds`` carries
            an ``ESCALATE`` verdict rather than the sub-threshold ``REVISE``.

        Raises:
            ValueError: When *medium* is unsupported.
        """
        validated = require_supported_medium(medium)
        evidence = self.gather_evidence(query, vault)
        rubric = self.contract.reflection_rubric if self.contract else None

        body = ""
        verdict: ReflectionVerdict = "ESCALATE"
        rounds = 0
        for attempt in range(1, self.max_rounds + 1):
            rounds = attempt
            body = self.voice.render(
                query, evidence, vault, medium=validated, contract=self.contract
            )
            result = self.reflection.review(
                body, evidence, rubric, contract=self.contract, vault=vault
            )
            verdict = result.decision
            if verdict != "REVISE":
                break

        # Never ship a sub-threshold draft: a still-REVISE verdict after the
        # round budget is exhausted escalates to a human (#473).
        if verdict == "REVISE":
            verdict = "ESCALATE"

        return AuthoredDraft(
            medium=validated,
            query=query,
            body=body,
            provenance=_claims_to_provenance(evidence.claims),
            verdict=verdict,
            rounds=rounds,
        )


def build_default_conductor(
    *,
    max_rounds: int,
    llm_client: AuthorLLMClient | None = None,
    contract: MediumContract | None = None,
) -> Conductor:
    """Build a conductor wired with the default stub collaborators.

    Args:
        max_rounds: Upper bound on voice/reflect rounds.
        llm_client: Optional network seam passed through to the conductor.
        contract: Optional medium contract passed through to the conductor.

    Returns:
        A ready-to-run :class:`Conductor`.
    """
    return Conductor(
        specialists=default_specialists(),
        voice=VoiceAgent(),
        reflection=ReflectionNode(),
        max_rounds=max_rounds,
        llm_client=llm_client,
        contract=contract,
    )


def run_author(
    *,
    medium: str,
    query: str,
    vault: Path,
    max_rounds: int | None = None,
) -> AuthoredDraft:
    """Author *query* for *medium* against *vault* with the default desk.

    Args:
        medium: The requested medium (only ``research`` is wired).
        query: The user query.
        vault: The vault to author from.
        max_rounds: Optional override for the round bound; defaults to the
            ``author.max_author_rounds`` config default.

    Returns:
        The shaped :class:`AuthoredDraft`.
    """
    from creek.config import AuthorConfig

    require_supported_medium(medium)
    contract = load_medium_contract(medium, vault)
    rounds = max_rounds if max_rounds is not None else AuthorConfig().max_author_rounds
    draft = build_default_conductor(max_rounds=rounds, contract=contract).run(
        medium=medium, query=query, vault=vault
    )
    return _enforce_chat_ceiling(draft)


class AuthorPlan(TypedDict):
    """The dry-run preview returned by :func:`plan_author` (FEAT-041 #460).

    Attributes:
        plan: Ordered pipeline step names.
        evidence: Counts of synthesized ``claims`` and ``source_fragments``.
    """

    plan: list[str]
    evidence: dict[str, int]


def plan_author(*, medium: str, query: str, vault: Path) -> AuthorPlan:
    """Return the pipeline plan + evidence summary without authoring (dry run).

    A lightweight preview for ``creek author --dry-run`` and the MCP verb's
    ``dry_run``: it runs the specialists and synthesize step but skips the
    voice/reflect loop, so no draft is produced.

    Args:
        medium: The requested medium.
        query: The user query.
        vault: The vault to gather evidence from.

    Returns:
        An :class:`AuthorPlan` with the step plan and evidence counts.

    Raises:
        ValueError: When *medium* is unsupported.
    """
    require_supported_medium(medium)
    contract = load_medium_contract(medium, vault)
    conductor = build_default_conductor(max_rounds=1, contract=contract)
    evidence = conductor.gather_evidence(query, vault)
    return {
        "plan": conductor.plan(),
        "evidence": {
            "claims": len(evidence.claims),
            "source_fragments": len(evidence.all_source_fragments()),
        },
    }


def _enforce_chat_ceiling(draft: AuthoredDraft) -> AuthoredDraft:
    """Truncate an over-length ``chat`` reply to :data:`CHAT_MAX_CHARS`.

    The chat medium's post-generation gate (FEAT-041 §5): a reply that reaches
    the caller must stay under the character ceiling. Other mediums pass
    through unchanged.

    Args:
        draft: The freshly authored draft.

    Returns:
        *draft* unchanged, or a copy with its body truncated to the ceiling.
    """
    if draft.medium != "chat" or len(draft.body) <= CHAT_MAX_CHARS:
        return draft
    truncated = draft.body[: CHAT_MAX_CHARS - 1].rstrip() + "…"
    return draft.model_copy(update={"body": truncated})
