"""Typed data models for the Creek Writing Desk (FEAT-041).

These are the shapes that flow through the author desk: specialists emit
:class:`EvidenceClaim` records (a claim traced to its source fragments),
the conductor aggregates them into an :class:`EvidenceBundle`, and one run
yields an :class:`AuthoredDraft`. Real retrieval/synthesis/judging fill these
shapes in later FEAT-041 issues; the skeleton populates them with mock data.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, computed_field

from creek.classify.weighted import WeightedDimension
from creek.compile.provenance import ProvenanceEntry
from creek.models import Dosage, Frequency, Mode, Phase

#: Mediums the author desk can produce. ``research``/``chat``/``essay``/
#: ``research-piece``/``book-report`` are wired; the others (how-to) arrive in
#: later issues.
Medium = Literal["research", "chat", "essay", "research-piece", "book-report"]

#: The reflection node's bounded verdict over a drafted body.
ReflectionVerdict = Literal["PASS", "REVISE", "ESCALATE"]


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
        paradoxes: Surfaced contradictions across fragments.
        overall_confidence: Aggregate confidence in ``[0.0, 1.0]``.
    """

    model_config = ConfigDict(frozen=True)

    frequencies: tuple[WeightedDimension[Frequency], ...] = ()
    phases: tuple[WeightedDimension[Phase], ...] = ()
    modes: tuple[WeightedDimension[Mode], ...] = ()
    dosages: tuple[WeightedDimension[Dosage], ...] = ()
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
        body: The drafted prose (mock text in the skeleton).
        provenance: Per-claim provenance entries, reusing the compile-layer
            :class:`~creek.compile.provenance.ProvenanceEntry` shape.
        verdict: The reflection node's verdict for this draft.
        rounds: How many voice/reflect rounds ran (``>= 1``).
    """

    model_config = ConfigDict(frozen=True)

    medium: Medium
    query: str
    body: str
    provenance: list[ProvenanceEntry] = Field(default_factory=list)
    verdict: ReflectionVerdict
    rounds: int = Field(ge=1)

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
