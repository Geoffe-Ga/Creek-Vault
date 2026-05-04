"""Pydantic v2 models for all Creek ontological primitives.

This module defines the data models for the six Creek ontological primitives:
Fragment, Thread, Eddy, Praxis, Decision, and WavelengthObservation.
It also provides supporting enums and nested classification models used
for the APTITUDE frequency framework and Archetypal Wavelength mapping.
"""

import uuid
from datetime import date, datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, computed_field

from creek.time import now_la, today_la

# ---- Enums ----


class Frequency(StrEnum):
    """APTITUDE frequency classification (F1-F10 plus unclassified)."""

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


class Phase(StrEnum):
    """Archetypal Wavelength phase within the six-phase cycle."""

    RISING = "rising"
    PEAKING = "peaking"
    WITHDRAWAL = "withdrawal"
    DIMINISHING = "diminishing"
    BOTTOMING_OUT = "bottoming_out"
    RESTORATION = "restoration"
    UNCLASSIFIED = "unclassified"


class Mode(StrEnum):
    """Engagement mode describing how a frequency is being experienced."""

    INHABIT = "inhabit"
    EXPRESS = "express"
    COLLABORATE = "collaborate"
    INTEGRATE = "integrate"
    ABSORB = "absorb"
    UNCLASSIFIED = "unclassified"


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
    """

    PUBLIC = "public"
    PERSONAL = "personal"
    INTIMATE = "intimate"
    UNCLASSIFIED = "unclassified"


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
