"""Voice agent for the Creek Writing Desk (FEAT-041, #471).

Renders a synthesized :class:`~creek.author.models.EvidenceBundle` in the vault
owner's voice. Two paths share one public :meth:`VoiceAgent.render`:

* **No client / no vault** — return a deterministic structured body that lists
  every grounded claim. This keeps offline runs (and the skeleton's smoke
  tests) green without a network seam.
* **Client + vault** — build a voice prompt from the ``creek-skills``
  voice-skill stack (voice-core + phase + register) plus the grounded evidence,
  and ask the injected :class:`~creek.author.client.AuthorLLMClient` to voice
  it. Research is voiced with a *light* touch — clarity over flourish, never
  inventing facts or dropping grounded content. Borrowed
  (``11-Other-Authors/``) claims are excluded from the owner-voiced material so
  the owner's voice never presents another author's words as its own.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from creek.author.skills import (
    find_phase_skill,
    find_register_skill,
    find_voice_core,
    read_skill,
)
from creek.care.guardrail import CARE_POLICY
from creek.generate.ontology_glossary import GLOSS_STEER

if TYPE_CHECKING:
    from pathlib import Path

    from creek.author.client import AuthorLLMClient
    from creek.author.models import EvidenceBundle, EvidenceClaim, Medium
    from creek.models import MediumContract

#: Mediums voiced with a light touch (clarity over flourish).
_LIGHT_VOICING_MEDIUMS: frozenset[str] = frozenset({"research", "research-piece"})

_LIGHT_TOUCH_NOTE: str = (
    "This is a research medium: voice with a light touch — clarity over "
    "flourish. Tighten and connect the evidence; do not embellish."
)


class VoiceAgent:
    """Render grounded evidence in the owner's voice (LLM or deterministic).

    Attributes:
        llm_client: Optional network seam. When ``None`` (the default), every
            render takes the deterministic path so offline runs work.
    """

    def __init__(self, *, llm_client: AuthorLLMClient | None = None) -> None:
        """Store the optional LLM client used for owner-voiced rendering.

        Args:
            llm_client: Optional client; ``None`` selects the deterministic
                path on every render.
        """
        self.llm_client = llm_client
        #: Token usage from the most recent LLM render, or ``None`` when the
        #: deterministic/no-client path ran. The conductor reads this to
        #: surface a run's cost and cache-hit rate (#474).
        self.last_usage: dict[str, int] | None = None

    def render(
        self,
        query: str,
        evidence: EvidenceBundle,
        vault: Path | None = None,
        *,
        medium: Medium | None = None,
        contract: MediumContract | None = None,
        client: AuthorLLMClient | None = None,
    ) -> str:
        """Render *evidence* into draft prose for *query* in the owner's voice.

        Args:
            query: The originating user query.
            evidence: The grounded evidence to render.
            vault: The vault whose ``creek-skills`` tree conditions the voice;
                ``None`` (or no client) takes the deterministic path.
            medium: The run medium, used to pick the voicing register
                (research = light touch).
            contract: The medium contract whose ``structure`` orders the
                grounded evidence in the prompt.
            client: A per-render client override (#661) — the conductor passes
                the tier-routed voice client here. ``None`` falls back to the
                agent's own :attr:`llm_client`.

        Returns:
            The voiced draft body. The LLM output when a client and vault are
            present and the model returns text; otherwise the deterministic
            body (also the fallback when the LLM returns empty text).
        """
        # Reset first so a deterministic render never leaks a prior LLM call's
        # usage if this agent instance is reused (#474 review).
        self.last_usage = None
        effective_client = client if client is not None else self.llm_client
        deterministic = _deterministic_body(query, evidence)
        if effective_client is None or vault is None:
            return deterministic
        static, dynamic = _split_voice_prompt(query, evidence, vault, medium, contract)
        completion = effective_client.complete_with_usage(dynamic, system=static)
        self.last_usage = completion.usage
        voiced = completion.text.strip()
        return voiced or deterministic


def _owner_claims(evidence: EvidenceBundle) -> list[EvidenceClaim]:
    """Return the owner's grounded claims, excluding borrowed-author claims.

    A claim with an ``author_slug`` is drawn from ``11-Other-Authors/``; it is
    already attributed and must not be voiced as the owner's own words, so it
    is dropped from the owner-voiced material here.
    """
    return [
        claim
        for claim in evidence.claims
        if claim.source_fragments and not claim.author_slug
    ]


def _deterministic_body(query: str, evidence: EvidenceBundle) -> str:
    """Return a non-empty markdown body listing every grounded claim.

    Borrowed-author claims are excluded so the owner-voiced body never presents
    another author's words as the owner's own. Falls back to the query heading
    alone when no owner claims remain.
    """
    lines = [f"# {query}", ""]
    lines.extend(f"- {claim.claim}" for claim in _owner_claims(evidence))
    return "\n".join(lines)


def _split_voice_prompt(
    query: str,
    evidence: EvidenceBundle,
    vault: Path,
    medium: Medium | None,
    contract: MediumContract | None,
) -> tuple[str, str]:
    """Split the owner-voice prompt into its static and dynamic halves.

    Prompt caching (#474) bills the static prefix once and re-reads it cheaply
    on repeat runs. The ``creek-skills`` voice-skill sections are identical
    across queries for a given vault, so they form the cached *static* block;
    the per-query evidence and ask form the *dynamic* user prompt. Splitting
    here changes no content the model sees — the union of static + dynamic is
    the same material the single-string prompt carried — only how it is billed.

    Returns:
        A ``(static, dynamic)`` pair. ``static`` is the joined skill sections
        (``""`` when the vault carries none); ``dynamic`` is the evidence block
        followed by the ask block.
    """
    static = "\n\n".join(
        [CARE_POLICY, *(part for part in _skill_sections(vault, evidence) if part)]
    )
    dynamic = "\n\n".join(
        part
        for part in (_evidence_section(evidence), _ask_section(query, medium, contract))
        if part
    )
    return static, dynamic


def _skill_sections(vault: Path, evidence: EvidenceBundle) -> list[str]:
    """Return the voice-core, phase, and register skill sections, in order.

    Missing skill files are skipped silently. The phase is taken from the
    synthesized ontology's dominant phase, and the register from its dominant
    voice register, when present (#502).
    """
    paths = [
        find_voice_core(vault),
        find_phase_skill(vault, _dominant_phase(evidence)),
        find_register_skill(vault, _dominant_register(evidence)),
    ]
    sections: list[str] = []
    for path in paths:
        if path is None:
            continue
        text = read_skill(path)
        if text:
            sections.append(text)
    return sections


def _dominant_phase(evidence: EvidenceBundle) -> str | None:
    """Return the dominant phase slug from the synthesized ontology, if any."""
    ontology = evidence.ontology
    if ontology is None or not ontology.phases:
        return None
    # Double ``.value`` is intentional: ``WeightedDimension.value`` is the enum
    # member (a ``Phase``), and *its* ``.value`` is the canonical slug string.
    return ontology.phases[0].value.value


def _dominant_register(evidence: EvidenceBundle) -> str | None:
    """Return the dominant voice-register slug from the ontology, if any."""
    ontology = evidence.ontology
    if ontology is None or not ontology.voice_registers:
        return None
    # Double ``.value``: the ``WeightedDimension.value`` enum member, then its
    # ``.value`` slug — see :func:`_dominant_phase`.
    return ontology.voice_registers[0].value.value


def _evidence_section(evidence: EvidenceBundle) -> str:
    """Return the grounded ``## Evidence`` block for the voice prompt.

    Each line pairs a grounded claim with its source fragment ids so the model
    can trace every statement; borrowed-author claims are already excluded.
    """
    lines = [
        f"- {claim.claim} [fragments: {', '.join(claim.source_fragments)}]"
        for claim in _owner_claims(evidence)
    ]
    body = "\n".join(lines) if lines else "(no grounded evidence)"
    return f"## Evidence (grounded)\n{body}"


def _ask_section(
    query: str,
    medium: Medium | None,
    contract: MediumContract | None,
) -> str:
    """Return the grounding-preserving ``## Ask`` block for the voice prompt.

    When a *contract* is present its ``structure`` is passed to the model as
    the section order to organise the draft into, so the medium's contract
    shapes the output rather than just documenting an intent.
    """
    lines = [
        "## Ask",
        f"Render the evidence below as a draft answering: {query}",
        "Write in my (the vault owner's) voice.",
        "Every statement must trace to the provided source fragments — do not "
        "invent facts and do not add claims.",
        "Do not assert biographical facts about me that are not present in the "
        "source fragments. You may draw connections between ideas, but never "
        "invent events, my upbringing, or another person's motives.",
        "Use the source fragments as your own voice and memory. Weave them in as "
        "your present-tense thinking — never reference that something came from a "
        "journal, note, or entry, and never narrate that you are quoting "
        "yourself. State the idea, not its provenance.",
        "Do not quote borrowed authors as my own words.",
        GLOSS_STEER,
    ]
    if contract is not None and contract.structure:
        order = " → ".join(contract.structure)
        lines.append(f"Organise the draft into these sections, in order: {order}.")
    if medium in _LIGHT_VOICING_MEDIUMS:
        lines.append(_LIGHT_TOUCH_NOTE)
    return "\n".join(lines)
