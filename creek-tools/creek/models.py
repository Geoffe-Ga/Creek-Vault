"""Pydantic v2 models for all Creek ontological primitives.

This module defines the data models for the six Creek ontological primitives:
Fragment, Thread, Eddy, Praxis, Decision, and WavelengthObservation.
It also provides supporting enums and nested classification models used
for the APTITUDE frequency framework and Archetypal Wavelength mapping.
"""

import uuid
import warnings
from datetime import UTC, date, datetime
from enum import StrEnum
from typing import Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field, computed_field

from creek.compile.provenance import CompileMethod, ProvenanceEntry
from creek.time import now_la, today_la

CompileTargetKind = Literal["thread", "eddy", "frequency_index"]
"""The compiled-page surfaces ``creek compile`` may target (FEAT-003)."""

FragmentLevel = Literal[
    "sentence",
    "paragraph",
    "subsection",
    "section",
    "document",
    "exchange",
    "burst",
    "session",
]
"""Structural level a :class:`Fragment` sits at within its source hierarchy.

The first four values (``sentence`` … ``section``) describe levels carved
*down* from a longer document by the FEAT-021 zoom-in splitter. The last
three (``exchange``, ``burst``, ``session``) describe levels stitched
*up* from short messages by the FEAT-022 zoom-out aggregator. ``document``
is the default for any flat ingestion — a chat conversation, an essay, a
note — and is therefore what every pre-FEAT-020 fragment is treated as
when it loads without an explicit ``level`` field.
"""


def _utc_now() -> datetime:
    """Return the current time in UTC (used as a Pydantic default factory).

    The compile engine writes ``compiled_at`` as UTC; this default
    keeps direct ``CompiledPage(...)`` constructions consistent so an
    operator who skips the engine doesn't silently get LA-local time.
    """
    return datetime.now(tz=UTC)


# ---- Enums ----


# INC-019 one-release migration aliases — see the INC doc for context.
_PHASE_LEGACY_ALIASES = {
    "origins": "rising",
    "cresting": "withdrawal",
    "receding": "diminishing",
    "composting": "restoration",
}

_MODE_LEGACY_ALIASES = {
    "solo": "inhabit",
    "dialogue": "express",
    "reflective": "integrate",
    "analytic": "collaborate",
}

_FREQUENCY_LEGACY_ALIASES = {
    "amplitude": "F1",
    "pitch": "F2",
}


_E = TypeVar("_E", bound=StrEnum)


def _legacy_alias_lookup(
    cls: type[_E],
    value: object,
    aliases: dict[str, str],
) -> _E | None:
    """Resolve a legacy INC-019 string to its canonical enum member, or None."""
    if not isinstance(value, str):
        return None
    canonical = aliases.get(value)
    if canonical is None:
        return None
    warnings.warn(
        f"{cls.__name__} value {value!r} is deprecated; use {canonical!r}. "
        "INC-019: support for legacy phase/mode/frequency names will be "
        "removed in the next minor release.",
        DeprecationWarning,
        stacklevel=3,
    )
    return cls(canonical)


class Frequency(StrEnum):
    """APTITUDE frequency F1..F10 (plus unclassified); see INC-019 for aliases."""

    F1 = "F1"
    F2 = "F2"
    F3 = "F3"
    F4 = "F4"
    F5 = "F5"
    F6 = "F6"
    F7 = "F7"
    F8 = "F8"
    F9 = "F9"
    F10 = "F10"
    UNCLASSIFIED = "unclassified"

    @classmethod
    def _missing_(cls, value: object) -> "Frequency | None":
        """Map legacy INC-019 wave-physics strings to canonical F-codes."""
        return _legacy_alias_lookup(cls, value, _FREQUENCY_LEGACY_ALIASES)


class Phase(StrEnum):
    """Archetypal Wavelength six-phase cycle; see INC-019 for aliases."""

    RISING = "rising"
    PEAKING = "peaking"
    WITHDRAWAL = "withdrawal"
    DIMINISHING = "diminishing"
    BOTTOMING_OUT = "bottoming_out"
    RESTORATION = "restoration"
    UNCLASSIFIED = "unclassified"

    @classmethod
    def _missing_(cls, value: object) -> "Phase | None":
        """Map legacy INC-019 drift phase strings to canonical phases."""
        return _legacy_alias_lookup(cls, value, _PHASE_LEGACY_ALIASES)


class Mode(StrEnum):
    """Engagement mode for a fragment; see INC-019 for legacy aliases."""

    INHABIT = "inhabit"
    EXPRESS = "express"
    COLLABORATE = "collaborate"
    INTEGRATE = "integrate"
    ABSORB = "absorb"
    UNCLASSIFIED = "unclassified"

    @classmethod
    def _missing_(cls, value: object) -> "Mode | None":
        """Map legacy INC-019 drift mode strings to canonical modes."""
        return _legacy_alias_lookup(cls, value, _MODE_LEGACY_ALIASES)


class Orientation(StrEnum):
    """Action-feeling orientation of the content."""

    DO = "do"
    FEEL = "feel"
    DO_FEEL = "do_feel"
    UNCLASSIFIED = "unclassified"


class Dosage(StrEnum):
    """Whether the frequency expression is medicine, toxic, or ambiguous."""

    MEDICINE = "medicine"
    TOXIC = "toxic"
    AMBIGUOUS = "ambiguous"
    UNCLASSIFIED = "unclassified"


class Color(StrEnum):
    """Spiral Dynamics color mapping for frequency visualization."""

    BEIGE = "beige"
    PURPLE = "purple"
    RED = "red"
    BLUE = "blue"
    ORANGE = "orange"
    GREEN = "green"
    YELLOW = "yellow"
    TEAL = "teal"
    ULTRAVIOLET = "ultraviolet"
    CLEAR_LIGHT = "clear_light"
    UNCLASSIFIED = "unclassified"


class VoiceRegister(StrEnum):
    """Voice register describing the tone of the content."""

    CONFESSIONAL = "confessional"
    ANALYTICAL = "analytical"
    PLAYFUL = "playful"
    PROPHETIC = "prophetic"
    INSTRUCTIONAL = "instructional"
    RAW = "raw"
    CONVERSATIONAL = "conversational"


class Confidence(StrEnum):
    """Confidence level of a classification or observation."""

    MUSING = "musing"
    EXPLORING = "exploring"
    FORMING = "forming"
    SETTLED = "settled"
    CONVICTION = "conviction"


class PraxisType(StrEnum):
    """Type of praxis (actionable insight)."""

    HABIT = "habit"
    PRACTICE = "practice"
    FRAMEWORK = "framework"
    INSIGHT = "insight"
    COMMITMENT = "commitment"


class PraxisStatus(StrEnum):
    """Lifecycle status of a praxis."""

    PROPOSED = "proposed"
    ACTIVE = "active"
    INTEGRATED = "integrated"
    RELEASED = "released"


class ReviewInterval(StrEnum):
    """How often a praxis should be reviewed."""

    DAILY = "daily"
    WEEKLY = "weekly"
    MONTHLY = "monthly"
    SEASONAL = "seasonal"
    AS_NEEDED = "as_needed"


class ThreadStatus(StrEnum):
    """Lifecycle status of a narrative thread."""

    ACTIVE = "active"
    DORMANT = "dormant"
    RESOLVED = "resolved"


class DecisionStatus(StrEnum):
    """Lifecycle status of a decision."""

    SENSING = "sensing"
    DELIBERATING = "deliberating"
    COMMITTING = "committing"
    ENACTED = "enacted"
    REFLECTING = "reflecting"


class PraxisPotential(StrEnum):
    """Whether a fragment has potential to become a praxis."""

    NONE = "none"
    LATENT = "latent"
    EXPLICIT = "explicit"


class SourcePlatform(StrEnum):
    """Platform from which a fragment was ingested."""

    CLAUDE = "claude"
    CHATGPT = "chatgpt"
    DISCORD = "discord"
    JOURNAL = "journal"
    ESSAY = "essay"
    CODE = "code"
    MARKDOWN = "markdown"
    EMAIL = "email"
    DOCUMENT = "document"
    IMAGE_OCR = "image_ocr"
    SPREADSHEET = "spreadsheet"
    PRESENTATION = "presentation"
    OTHER = "other"


class Authorship(StrEnum):
    """Authorship classification for a fragment's content.

    Determines whose words a fragment contains, used by the
    :class:`~creek.clean.context.ContextExtractor` to apply non-user
    content handling rules.
    """

    SELF = "self"
    AI = "ai"
    OTHER = "other"
    COLLABORATIVE = "collaborative"


class PrivacyTier(StrEnum):
    """Privacy classification tier for fragment content.

    Controls visibility and handling restrictions. ``intimate`` content
    is reserved exclusively for self-authored fragments.

    Naming history (INC-003): the canonical name is ``OPEN``
    (``"open"``) per ontology §13.2 — "openly publishable", not
    "internet-public". The legacy value ``"public"`` is accepted on
    input via :meth:`_missing_` and silently mapped to ``OPEN``, with
    a :class:`DeprecationWarning` so an operator running against an
    older vault knows to migrate. Plan removal in the next minor
    version.
    """

    OPEN = "open"
    PERSONAL = "personal"
    INTIMATE = "intimate"
    UNCLASSIFIED = "unclassified"

    @classmethod
    def _missing_(cls, value: object) -> "PrivacyTier | None":
        """Map the deprecated ``"public"`` string to :attr:`OPEN`.

        Old vaults serialised the tier as ``public``. INC-003 renamed
        the canonical value to ``open``; this hook keeps those vaults
        loadable for one release while emitting a
        :class:`DeprecationWarning` that names the migration path.
        Any other unknown value still raises ``ValueError`` from the
        StrEnum constructor.
        """
        if isinstance(value, str) and value == "public":
            import warnings

            warnings.warn(
                "PrivacyTier value 'public' is deprecated; use 'open'. "
                "INC-003: support for 'public' will be removed in the "
                "next minor release.",
                DeprecationWarning,
                stacklevel=2,
            )
            return cls.OPEN
        return None


# ---- ID Generation Helpers ----


def synthetic_fragment_id() -> str:
    """Generate a synthetic (random) fragment ID with prefix 'frag-'.

    Reserved for fragments without a deterministic ``(source, timestamp,
    content)`` triple — for example, synthesised praxis-derived fragments
    or test fixtures. Production ingestors must use the deterministic
    :func:`creek.ingest.base.generate_fragment_id` instead so re-running
    the pipeline against the same source is idempotent.

    The width matches ``generate_fragment_id`` (12 hex chars) so the two
    namespaces are visually indistinguishable downstream.

    Returns:
        A random ID string in the format ``frag-XXXXXXXXXXXX``.
    """
    return f"frag-{uuid.uuid4().hex[:12]}"


def _generate_thread_id() -> str:
    """Generate a unique thread ID with prefix 'thread-'."""
    return f"thread-{uuid.uuid4().hex[:8]}"


def _generate_eddy_id() -> str:
    """Generate a unique eddy ID with prefix 'eddy-'."""
    return f"eddy-{uuid.uuid4().hex[:8]}"


def _generate_praxis_id() -> str:
    """Generate a unique praxis ID with prefix 'praxis-'."""
    return f"praxis-{uuid.uuid4().hex[:8]}"


def _generate_decision_id() -> str:
    """Generate a unique decision ID with prefix 'decision-'."""
    return f"decision-{uuid.uuid4().hex[:8]}"


def _generate_candidate_id() -> str:
    """Generate a unique decision candidate ID with prefix 'candidate-'."""
    return f"candidate-{uuid.uuid4().hex[:8]}"


def _generate_wave_id() -> str:
    """Generate a unique wavelength observation ID with prefix 'wave-'."""
    return f"wave-{uuid.uuid4().hex[:8]}"


def _generate_sync_id() -> str:
    """Generate a unique synchronicity ID with prefix 'sync-'."""
    return f"sync-{uuid.uuid4().hex[:8]}"


# ---- Nested Models ----


class FragmentSource(BaseModel):
    """Source metadata for a fragment, describing where it was ingested from."""

    model_config = ConfigDict(use_enum_values=True)

    platform: SourcePlatform
    original_file: str | None = None
    original_encoding: str | None = None
    conversation_id: str | None = None
    channel: str | None = None
    interlocutor: str | None = None
    author: Authorship = Authorship.SELF


class FrequencyClassification(BaseModel):
    """APTITUDE frequency classification with primary and secondary frequencies."""

    model_config = ConfigDict(use_enum_values=True)

    primary: Frequency = Frequency.UNCLASSIFIED
    secondary: list[Frequency] = Field(default_factory=list)


class WavelengthClassification(BaseModel):
    """Archetypal Wavelength classification for phase, mode, and related axes."""

    model_config = ConfigDict(use_enum_values=True)

    phase: Phase = Phase.UNCLASSIFIED
    mode: Mode = Mode.UNCLASSIFIED
    orientation: Orientation = Orientation.UNCLASSIFIED
    dosage: Dosage = Dosage.UNCLASSIFIED
    color: Color = Color.UNCLASSIFIED
    descriptor: str = ""


class VoiceClassification(BaseModel):
    """Voice register and confidence classification for a fragment."""

    model_config = ConfigDict(use_enum_values=True)

    voice_register: VoiceRegister | None = None
    confidence: Confidence | None = None


# ---- Primitive Models ----


class Fragment(BaseModel):
    """An atomic content unit — the fundamental building block of the Creek system.

    Fragments are ingested from various sources and classified along
    frequency, wavelength, and voice dimensions.
    """

    model_config = ConfigDict(use_enum_values=True)

    type: str = "fragment"
    id: str
    title: str
    source: FragmentSource
    created: datetime = Field(default_factory=now_la)
    ingested: datetime = Field(default_factory=now_la)
    frequency: FrequencyClassification = Field(
        default_factory=FrequencyClassification,
    )
    wavelength: WavelengthClassification = Field(
        default_factory=WavelengthClassification,
    )
    voice: VoiceClassification = Field(default_factory=VoiceClassification)
    emotional_texture: list[str] = Field(default_factory=list)
    threads: list[str] = Field(default_factory=list)
    eddies: list[str] = Field(default_factory=list)
    praxis_potential: PraxisPotential = PraxisPotential.NONE
    privacy_tier: PrivacyTier = PrivacyTier.UNCLASSIFIED
    context: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    # FEAT-020 hierarchical fragment data model. Flat (pre-FEAT-020)
    # fragments load as root documents: ``parent_id=None``,
    # ``child_ids=[]``, ``level="document"``, ``structural_path=[]``.
    # These four fields are direction-agnostic — a parent may be the
    # level *above* its children whether the children were carved out
    # by the FEAT-021 zoom-in splitter or stitched together by the
    # FEAT-022 zoom-out aggregator.
    parent_id: str | None = None
    child_ids: list[str] = Field(default_factory=list)
    level: FragmentLevel = "document"
    structural_path: list[str] = Field(default_factory=list)

    # BUG-009: the ``[prop-decorator]`` suppression below is a known
    # mypy / Pydantic-v2 limitation when stacking ``@computed_field``
    # over ``@property`` — see
    # https://github.com/pydantic/pydantic/issues/6710. The bytes-on-disk
    # behaviour is correct; mypy just can't model the descriptor stack.
    @computed_field  # type: ignore[prop-decorator]
    @property
    def voice_proxy_eligible(self) -> bool:
        """Whether this fragment may feed voice-proxy generation.

        Derived (BUG-009) so the flag cannot drift from ``privacy_tier``
        and ``source.author``: only self-authored, non-INTIMATE fragments
        are eligible. INTIMATE content is excluded per ontology §13.2,
        and AI / collaborator / other-authored content is excluded per
        the universal-constraints rule in :mod:`creek.clean.context`.
        """
        return (
            self.privacy_tier != PrivacyTier.INTIMATE
            and self.source.author == Authorship.SELF
        )


class DecisionCandidate(BaseModel):
    """A candidate fragment flagged as decision-relevant before full Decision creation.

    Decision candidates are detected by keyword and pattern analysis of fragments.
    They hold the source fragment reference, matched keywords, the detection method,
    and a confidence score indicating the strength of the signal.
    """

    model_config = ConfigDict(use_enum_values=True)

    id: str = Field(default_factory=_generate_candidate_id)
    fragment_id: str
    fragment_title: str
    matched_keywords: list[str] = Field(default_factory=list)
    detection_method: str = ""
    confidence_score: float = 0.0
    wavelength_phase_at_detection: str = ""
    frequency_context: list[Frequency] = Field(default_factory=list)


class Thread(BaseModel):
    """A narrative current — a recurring theme or pattern across fragments.

    Threads track the evolution of ideas and concerns over time.
    """

    model_config = ConfigDict(use_enum_values=True)

    type: str = "thread"
    id: str = Field(default_factory=_generate_thread_id)
    title: str
    status: ThreadStatus = ThreadStatus.ACTIVE
    first_seen: date = Field(default_factory=today_la)
    last_seen: date = Field(default_factory=today_la)
    frequency_affinity: list[Frequency] = Field(default_factory=list)
    fragment_count: int = 0
    description: str = ""
    tags: list[str] = Field(default_factory=list)


class Eddy(BaseModel):
    """A topic cluster — a convergence point where multiple threads intersect.

    Eddies represent areas of concentrated attention and meaning.
    """

    model_config = ConfigDict(use_enum_values=True)

    type: str = "eddy"
    id: str = Field(default_factory=_generate_eddy_id)
    title: str
    formed: date = Field(default_factory=today_la)
    fragment_count: int = 0
    threads: list[str] = Field(default_factory=list)
    description: str = ""
    tags: list[str] = Field(default_factory=list)


class Praxis(BaseModel):
    """An actionable insight — knowledge distilled into practice.

    Praxis items track habits, practices, frameworks, and commitments
    that emerge from the knowledge organization process.
    """

    model_config = ConfigDict(use_enum_values=True)

    type: str = "praxis"
    id: str = Field(default_factory=_generate_praxis_id)
    title: str
    frequency: list[Frequency] = Field(default_factory=list)
    praxis_type: PraxisType = PraxisType.INSIGHT
    derived_from: list[str] = Field(default_factory=list)
    status: PraxisStatus = PraxisStatus.PROPOSED
    review_interval: ReviewInterval = ReviewInterval.AS_NEEDED
    tags: list[str] = Field(default_factory=list)


class Decision(BaseModel):
    """A decision point — tracking the lifecycle of significant choices.

    Decisions move through sensing, deliberating, committing, enacting,
    and reflecting phases.
    """

    model_config = ConfigDict(use_enum_values=True)

    type: str = "decision"
    id: str = Field(default_factory=_generate_decision_id)
    title: str
    status: DecisionStatus = DecisionStatus.SENSING
    opened: date = Field(default_factory=today_la)
    decided: date | None = None
    frequency_context: list[Frequency] = Field(default_factory=list)
    wavelength_phase_at_opening: str = ""
    relevant_threads: list[str] = Field(default_factory=list)
    relevant_praxis: list[str] = Field(default_factory=list)
    options: list[str] = Field(default_factory=list)
    criteria: list[str] = Field(default_factory=list)
    outcome: str = ""
    tags: list[str] = Field(default_factory=list)


class WavelengthObservation(BaseModel):
    """A wavelength observation — a snapshot of the current wavelength state.

    Observations track phase, mode, and dosage over time to reveal
    the archetypal wavelength pattern.
    """

    model_config = ConfigDict(use_enum_values=True)

    type: str = "wavelength_observation"
    id: str = Field(default_factory=_generate_wave_id)
    date: date
    phase: Phase = Phase.UNCLASSIFIED
    mode: Mode = Mode.UNCLASSIFIED
    dosage: Dosage = Dosage.UNCLASSIFIED
    confidence: Confidence = Confidence.MUSING
    notes: str = ""
    fragment_refs: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)


class Synchronicity(BaseModel):
    """A meaningful coincidence between two cross-source fragments.

    Synchronicities surface when the linking pass discovers fragments from
    very different sources and times that are semantically near-identical.
    They live in ``10-Liminal/Synchronicities/`` and are intended as
    reflective prompts rather than firm knowledge links.

    Attributes:
        id: Unique identifier prefixed ``sync-``.
        fragment_a_id: ID of the earlier fragment in the pair.
        fragment_b_id: ID of the later fragment in the pair.
        similarity: Cosine similarity score between the two fragments
            (must be > 0.9 to qualify as a synchronicity).
        time_gap_days: Absolute gap in days between the fragments'
            creation timestamps.
        source_a: Source platform of the first fragment.
        source_b: Source platform of the second fragment (must differ
            from ``source_a``).
        tags: Obsidian tags applied to the synchronicity note.
    """

    model_config = ConfigDict(use_enum_values=True)

    type: str = "synchronicity"
    id: str = Field(default_factory=_generate_sync_id)
    fragment_a_id: str
    fragment_b_id: str
    similarity: float
    time_gap_days: int
    source_a: SourcePlatform
    source_b: SourcePlatform
    tags: list[str] = Field(default_factory=lambda: ["synchronicity"])


class CompiledPage(BaseModel):
    """A compiled-layer synthesis page produced by ``creek compile`` (FEAT-003).

    Compile rolls fragments from ``01-Fragments/`` up into Threads,
    Eddies, and per-frequency index notes. The page's YAML frontmatter
    carries one :class:`ProvenanceEntry` per claim so every assertion
    on the page traces back to the fragment(s) that produced it.

    Attributes:
        target_kind: Which compiled-layer surface this page lives on —
            ``"thread"``, ``"eddy"``, or ``"frequency_index"``.
        target_id: Stable identifier of the synthesis target (e.g.
            ``"thread-systems"``).
        title: Human-readable title rendered into the page heading.
        body: Markdown body of the synthesis. Paradoxes are *never*
            flattened into this body — they route to the side-channel
            paradox log instead (ontology spec §10.2).
        provenance: Per-claim provenance entries; merged across
            idempotent re-runs.
        compiled_at: UTC timestamp of the most recent compile run.
        compile_method: How the page's claims were produced — one of
            ``"rules"``, ``"llm"``, or ``"manual"``.
    """

    model_config = ConfigDict(use_enum_values=True)

    type: str = "compiled_page"
    target_kind: CompileTargetKind
    target_id: str
    title: str
    body: str = ""
    provenance: list[ProvenanceEntry] = Field(default_factory=list)
    compiled_at: datetime = Field(default_factory=_utc_now)
    compile_method: CompileMethod = "llm"
