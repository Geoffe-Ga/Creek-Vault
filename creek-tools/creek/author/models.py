"""Typed data models for the Creek Writing Desk (FEAT-041).

These are the shapes that flow through the author desk: specialists emit
:class:`EvidenceClaim` records (a claim traced to its source fragments),
the conductor aggregates them into an :class:`EvidenceBundle`, and one run
yields an :class:`AuthoredDraft`. The desk's deterministic
retrieval/synthesis/judging populate these shapes with real data; the voice
agent fills the draft body live when a provider is available and with a
deterministic rendering otherwise.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, computed_field

from creek.classify.weighted import WeightedDimension
from creek.compile.provenance import ProvenanceEntry
from creek.models import Dosage, Frequency, Mode, Phase, VoiceRegister

#: Mediums the author desk can produce. ``research``/``chat``/``essay``/
#: ``research-piece``/``book-report``/``how-to`` are all wired — ``how-to``
#: completes the medium set.
Medium = Literal["research", "chat", "essay", "research-piece", "book-report", "how-to"]

#: The reflection node's bounded verdict over a drafted body.
ReflectionVerdict = Literal["PASS", "REVISE", "ESCALATE"]

#: Severity of a single reflection finding (#473). ``HIGH`` marks a hard-gate
#: breach (citation/privacy), ``MID`` a softer rubric divergence, ``LOW`` a hint.
FindingSeverity = Literal["LOW", "MID", "HIGH"]

#: The research-rubric dimensions a reflection finding can fire on (#473).
#: Typed so strict mypy catches a mistyped dimension at the construction site.
#: ``biographical_grounding`` (#515) is the seventh: a HARD gate flagging a
#: first-person biographical claim that no source supports.
FindingDimension = Literal[
    "voice_fidelity",
    "ontological_accuracy",
    "citation_completeness",
    "privacy_compliance",
    "paradox_preservation",
    "attribution_correctness",
    "biographical_grounding",
    "unglossed_jargon",
]


class ReflectionFinding(BaseModel):
    """One scored defect the reflection node found in a drafted body (#473).

    Attributes:
        dimension: Which of the six rubric dimensions fired (e.g.
            ``"citation_completeness"``).
        severity: How serious the defect is — ``HIGH`` for a hard-gate
            breach, ``MID`` for a rubric divergence, ``LOW`` for a hint.
        message: A human-readable explanation naming the concrete defect.
    """

    model_config = ConfigDict(frozen=True)

    dimension: FindingDimension
    severity: FindingSeverity
    message: str


class ReflectionResult(BaseModel):
    """The structured outcome of judging one drafted body (#473).

    Attributes:
        decision: The bounded verdict — ``PASS`` (ship), ``REVISE`` (one or
            more findings; retry), or ``ESCALATE`` (cannot author at all).
        findings: Every :class:`ReflectionFinding` the checks raised; empty
            on a clean ``PASS``.
    """

    model_config = ConfigDict(frozen=True)

    decision: ReflectionVerdict
    findings: list[ReflectionFinding] = Field(default_factory=list)


class EvidenceClaim(BaseModel):
    """A single structured claim from a specialist, traced to fragments.

    Specialists return *structured evidence*, never free prose: each claim is
    one assertion paired with the fragment ids that support it.

    Attributes:
        claim: The asserted statement, in one short sentence.
        source_fragments: Ordered fragment ids backing the claim.
    """

    model_config = ConfigDict(frozen=True)

    claim: str
    source_fragments: list[str] = Field(default_factory=list)
    # When a claim is drawn from `11-Other-Authors/`, the author slug travels
    # with it so downstream citation can attribute it correctly.
    author_slug: str | None = None


class WalkStats(BaseModel):
    """Bounds-tracking stats for a Graph agent's backlink walk.

    Attributes:
        max_depth: The deepest hop reached from the seed (``0`` = seed only).
        fragments_visited: How many fragments the bounded walk visited.
    """

    model_config = ConfigDict(frozen=True)

    max_depth: int = 0
    fragments_visited: int = 0


class OntologyParadox(BaseModel):
    """A surfaced (never resolved) contradiction across two fragments.

    The Ontology specialist *names* a tension rather than collapsing it: a
    paradox carries the contributing fragment ids and a neutral, canonical
    description of the contradiction so downstream voicing keeps both
    conflicting signals visible (FEAT-041 §6; Ontology §10.2).

    Attributes:
        kind: Which contradiction fired — ``"dosage"``, ``"phase"``, or
            ``"confidence"``.
        fragment_ids: The fragment ids in tension, in stable order.
        description: A neutral one-sentence statement of the contradiction;
            it names the tension, never resolves it.
    """

    model_config = ConfigDict(frozen=True)

    kind: Literal["dosage", "phase", "confidence"]
    fragment_ids: tuple[str, ...] = ()
    description: str = ""


class OntologyAnalysis(BaseModel):
    """Structured ontological analysis from the Ontology specialist (FEAT-041 §4.1).

    Each axis is a weight-descending tuple of canonical
    :class:`~creek.classify.weighted.WeightedDimension` entries (canonical
    taxonomy only — no aliases, INC-019). Paradoxes are surfaced, not
    resolved.

    Attributes:
        frequencies: Weighted APTITUDE frequencies (F1-F10) the corpus
            resonates with.
        phases: Weighted Archetypal Wavelength phases.
        modes: Weighted engagement modes.
        dosages: Weighted dosage framings (medicine/toxic/ambiguous).
        voice_registers: Weighted voice registers the corpus speaks in
            (confessional/analytical/playful/…).
        paradoxes: Surfaced contradictions across fragments.
        overall_confidence: Aggregate confidence in ``[0.0, 1.0]``.
    """

    model_config = ConfigDict(frozen=True)

    frequencies: tuple[WeightedDimension[Frequency], ...] = ()
    phases: tuple[WeightedDimension[Phase], ...] = ()
    modes: tuple[WeightedDimension[Mode], ...] = ()
    dosages: tuple[WeightedDimension[Dosage], ...] = ()
    voice_registers: tuple[WeightedDimension[VoiceRegister], ...] = ()
    paradoxes: tuple[OntologyParadox, ...] = ()
    overall_confidence: float = 0.0


class EvidenceBundle(BaseModel):
    """The aggregated evidence the conductor hands to the voice agent.

    Attributes:
        claims: Every :class:`EvidenceClaim` gathered across specialists.
        walk_stats: Set by the Graph agent — the bounds its backlink walk hit.
        ontology: Set by the Ontology agent — structured ontological analysis.
    """

    model_config = ConfigDict(frozen=True)

    claims: list[EvidenceClaim] = Field(default_factory=list)
    walk_stats: WalkStats | None = None
    ontology: OntologyAnalysis | None = None

    def all_source_fragments(self) -> list[str]:
        """Return the order-preserving, deduplicated union of claim fragments.

        Returns:
            Fragment ids in first-seen order, with duplicates removed.
        """
        seen: dict[str, None] = {}
        for claim in self.claims:
            for fragment_id in claim.source_fragments:
                seen.setdefault(fragment_id, None)
        return list(seen)


class AuthoredDraft(BaseModel):
    """The shaped output of one author-desk run.

    Attributes:
        medium: The medium the draft was authored for.
        query: The originating user query.
        body: The drafted prose.
        provenance: Per-claim provenance entries, reusing the compile-layer
            :class:`~creek.compile.provenance.ProvenanceEntry` shape.
        verdict: The reflection node's verdict for this draft.
        rounds: How many voice/reflect rounds ran (``>= 1``).
        findings: The reflection findings from the final round. Carried so an
            ``ESCALATE`` (or ``REVISE``) verdict is actionable — a human can see
            exactly which dimensions failed rather than being told only that the
            draft was escalated.
        usage: Token-usage counts from the voice LLM call (``input_tokens``,
            ``output_tokens`` and, with prompt caching, the ``cache_*`` reads),
            or ``None`` when the run took the deterministic/offline path. Lets a
            caller observe a run's cost and cache-hit rate (#474).
    """

    model_config = ConfigDict(frozen=True)

    medium: Medium
    query: str
    body: str
    provenance: list[ProvenanceEntry] = Field(default_factory=list)
    verdict: ReflectionVerdict
    rounds: int = Field(ge=1)
    findings: list[ReflectionFinding] = Field(default_factory=list)
    usage: dict[str, int] | None = None

    # BUG-009: the ``[prop-decorator]`` suppression is the known mypy /
    # Pydantic-v2 limitation when stacking ``@computed_field`` over
    # ``@property`` — see https://github.com/pydantic/pydantic/issues/6710.
    # Serializing the alias is correct; mypy just can't model the descriptor
    # stack. Matches the existing carve-out on ``Fragment.voice_proxy_eligible``.
    @computed_field  # type: ignore[prop-decorator]
    @property
    def rendered_text(self) -> str:
        """The rendered draft text (alias for :attr:`body`), included in dumps."""
        return self.body


ZERO_EVIDENCE_WARNING = (
    "No grounded evidence was found for this query, so the draft stands on "
    "nothing from your vault. The usual cause is an unclassified corpus: at "
    "the default `open` ceiling a fragment with no concrete privacy_tier is "
    "excluded from evidence gathering (#1079), so a freshly-ingested vault "
    "reads as empty. Run `creek classify` over the vault and author again, "
    "or raise the ceiling with --include-tier if you intend to draft from "
    "unclassified material."
)
"""Warning emitted when evidence gathering returns zero grounded claims.

Issue #1261. The filter itself is correct and deliberately unchanged -- #1079
settled that a missing tier resolves restrictively, and reopening that is a
privacy-posture decision, not a UX one. What was wrong is that the command
said nothing: an operator with a freshly-ingested vault got a
confident-looking artefact built on no evidence, with the only hint being a
lowercase ``(no grounded evidence)`` fallback inside the body itself
(``creek/author/voice.py:222``).

Defined once, beside the model it describes, and rendered by both the CLI and
its MCP twin so the two cannot drift into wording the same condition
differently -- the failure #1362 records as four unlinked copies of one string.
"""


def has_zero_evidence(draft: AuthoredDraft) -> bool:
    """Return whether *draft* gathered no grounded provenance at all.

    Keyed on provenance rather than on ``verdict``: escalation is routine on
    this surface (an empty vault escalates, and any unresolved soft finding
    escalates once the round budget runs out), so a verdict test would fire on
    ordinary grounded drafts. Zero provenance is the precise condition #1261
    is about.

    Args:
        draft: The finished draft to inspect.

    Returns:
        ``True`` when the draft cites no provenance entries.
    """
    return not draft.provenance
