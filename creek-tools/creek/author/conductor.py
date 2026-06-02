"""The Conductor that orchestrates the Creek Writing Desk (FEAT-041, #455).

The conductor drives the end-to-end flow: each specialist gathers structured
evidence, the bundles are synthesized, the voice agent renders a draft, and the
reflection node judges it — looping on ``REVISE`` up to ``max_rounds`` before
returning a shaped :class:`~creek.author.models.AuthoredDraft`. In the skeleton
every collaborator is a typed stub; later issues swap in real implementations
behind these same seams.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Protocol, cast

from creek.author.agents import Specialist, default_specialists
from creek.author.contracts import load_medium_contract
from creek.author.models import (
    AuthoredDraft,
    EvidenceBundle,
    EvidenceClaim,
    Medium,
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

#: Mediums the conductor will run. Only ``research`` is wired in the skeleton.
SUPPORTED_MEDIUMS: frozenset[str] = frozenset({"research"})

#: Fixed pipeline steps that follow the specialist roster, in order.
_DOWNSTREAM_STEPS: tuple[str, ...] = ("synthesize", "voice", "reflect")

#: Excerpt length kept on each provenance entry's ``claim_excerpt``.
_EXCERPT_LEN: int = 80


class VoiceRenderer(Protocol):
    """Anything that renders evidence into a draft body."""

    def render(self, query: str, evidence: EvidenceBundle) -> str:
        """Return draft prose for *query* from *evidence*."""
        ...


class Reflector(Protocol):
    """Anything that judges a draft body against its evidence."""

    def review(
        self,
        body: str,
        evidence: EvidenceBundle,
        rubric: dict[str, float] | None = None,
    ) -> ReflectionVerdict:
        """Return a verdict for *body* given *evidence* and the medium *rubric*."""
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
        msg = (
            f"Unsupported medium {medium!r}; only the 'research' medium is "
            "wired in the Writing Desk skeleton (FEAT-041)."
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
        """Merge per-specialist *bundles* into one (the ``synthesize`` step).

        Args:
            bundles: One evidence bundle per specialist.

        Returns:
            A single :class:`EvidenceBundle` carrying every claim.
        """
        claims: list[EvidenceClaim] = []
        for bundle in bundles:
            claims.extend(bundle.claims)
        return EvidenceBundle(claims=claims)

    def run(self, *, medium: str, query: str, vault: Path) -> AuthoredDraft:
        """Run the full author pipeline and return a shaped draft.

        Args:
            medium: The requested medium (only ``research`` is wired).
            query: The user query.
            vault: The vault to author from.

        Returns:
            The :class:`AuthoredDraft` with mock provenance and a verdict.

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
            body = self.voice.render(query, evidence)
            verdict = self.reflection.review(body, evidence, rubric)
            if verdict != "REVISE":
                break

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
    return build_default_conductor(max_rounds=rounds, contract=contract).run(
        medium=medium, query=query, vault=vault
    )
