"""Pydantic v2 models for all Creek ontological primitives.

This module defines the data models for the six Creek ontological primitives:
Fragment, Thread, Eddy, Praxis, Decision, and WavelengthObservation.
It also provides supporting enums and nested classification models used
for the APTITUDE frequency framework and Archetypal Wavelength mapping.
"""

import logging
import uuid
import warnings
from contextlib import suppress
from datetime import UTC, date, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Literal, TypeVar, get_args

from pydantic import BaseModel, ConfigDict, Field, computed_field, field_validator

from creek.compile.provenance import CompileMethod, ProvenanceEntry
from creek.time import now_la, today_la

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    # Type-only import to expose :class:`WeightedFragmentClassification`
    # to mypy / IDEs. At runtime the symbol is resolved by the late
    # import + ``Fragment.model_rebuild`` at the bottom of this module;
    # a runtime import here would re-introduce the circular import
    # :mod:`creek.classify.weighted` imports back into :mod:`creek.models`.
    from creek.classify.weighted import WeightedFragmentClassification

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
    SUBSTACK = "substack"
    OTHER = "other"


class SourceKind(StrEnum):
    """Coarse semantic category for a fragment's source.

    Where :class:`SourcePlatform` answers *where the bytes came from*
    (Substack, ChatGPT, Discord, …), ``SourceKind`` answers *what kind
    of material it is* — a published essay versus a private journal
    entry, a chat message versus a code file. Downstream surfaces use
    the kind to make policy decisions (public-by-design vs.
    private-by-default redaction, voice-proxy register selection,
    folder routing) independently of which platform produced it.
    """

    WRITING = "writing"
    UNCLASSIFIED = "unclassified"


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


# ---- Author attribution (FEAT-041 §7.2) ----

AuthorKind = Literal["human_source", "ai_as_user", "collaborator"]
"""Who an other-author is: an external human, AI output the owner endorses as
their own interests, or a co-author."""

Representativeness = Literal["self", "endorsed", "aspirational", "reference"]
"""How closely an author's material stands for the vault owner's own views."""

# Derive the allowed-value sets from the Literal types so they can never drift
# out of sync when a new kind/representativeness is added.
_AUTHOR_KINDS: frozenset[str] = frozenset(get_args(AuthorKind))
_REPRESENTATIVENESS: frozenset[str] = frozenset(get_args(Representativeness))


def _warn_fail_closed(
    field: str,
    value: object,
    fallback: str,
    *,
    reason: str = "is not a recognised value",
) -> None:
    """Log a fail-closed warning, distinguishing a null value from an invalid one.

    Args:
        field: The manifest field being coerced.
        value: The offending input value.
        fallback: The safe default the field resolves to.
        reason: Why a non-null *value* was rejected.
    """
    if value is None:
        logger.warning("'%s' is null; failing closed to %r.", field, fallback)
    else:
        logger.warning(
            "%s %r %s; failing closed to %r.", field, value, reason, fallback
        )


def _safe_float(value: object) -> float | None:
    """Return *value* as a float, or ``None`` when it is not a real number.

    ``bool`` is rejected (it is a misleading ``int`` subclass); strings are
    parsed leniently so a hand-typed manifest still loads.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


class AuthorManifest(BaseModel):
    """Attribution manifest for an ``11-Other-Authors/<slug>/`` entry (FEAT-041 §7.2).

    Parsed from an ``_author.md`` frontmatter block. Every field *fails
    closed*: a malformed value resolves to a safe default (no voice influence,
    attribution required, the default OPEN privacy tier) rather than raising,
    so a hand-edited manifest can never crash the pipeline.

    Attributes:
        author_slug: Identity key; the loader sets this from the folder name.
        display_name: Human-readable name for citations.
        author_kind: External human, AI-as-user, or collaborator.
        voice_weight: Allowed voice influence in ``[0.0, 1.0]``; 0.0 by default.
        representativeness: How closely this stands for the owner's views.
        default_privacy_tier: Default tier for this author's captured content.
        attribution_required: Whether output drawing on this author must cite it.
        notes: Free-form capture rationale.
    """

    model_config = ConfigDict(use_enum_values=True, extra="ignore")

    author_slug: str
    display_name: str = ""
    author_kind: AuthorKind = "human_source"
    voice_weight: float = 0.0
    representativeness: Representativeness = "reference"
    default_privacy_tier: PrivacyTier = PrivacyTier.OPEN
    attribution_required: bool = True
    notes: str = ""

    @field_validator("author_kind", mode="before")
    @classmethod
    def _coerce_author_kind(cls, value: object) -> str:
        """Fail closed to ``human_source`` for an unrecognised author kind."""
        if isinstance(value, str) and value in _AUTHOR_KINDS:
            return value
        _warn_fail_closed("author_kind", value, "human_source")
        return "human_source"

    @field_validator("representativeness", mode="before")
    @classmethod
    def _coerce_representativeness(cls, value: object) -> str:
        """Fail closed to ``reference`` for an unrecognised representativeness."""
        if isinstance(value, str) and value in _REPRESENTATIVENESS:
            return value
        _warn_fail_closed("representativeness", value, "reference")
        return "reference"

    @field_validator("voice_weight", mode="before")
    @classmethod
    def _coerce_voice_weight(cls, value: object) -> float:
        """Fail closed to 0.0 for a non-numeric or out-of-range voice weight."""
        weight = _safe_float(value)
        if weight is None or not 0.0 <= weight <= 1.0:
            _warn_fail_closed(
                "voice_weight", value, "0.0", reason="is not a number in [0, 1]"
            )
            return 0.0
        return weight

    @field_validator("default_privacy_tier", mode="before")
    @classmethod
    def _coerce_privacy_tier(cls, value: object) -> PrivacyTier:
        """Fail closed to the default OPEN tier for an unrecognised privacy tier."""
        if isinstance(value, PrivacyTier):
            return value
        if isinstance(value, str):
            with suppress(ValueError):
                return PrivacyTier(value)
        _warn_fail_closed("default_privacy_tier", value, "open")
        return PrivacyTier.OPEN

    @field_validator("attribution_required", mode="before")
    @classmethod
    def _coerce_attribution_required(cls, value: object) -> bool:
        """Fail closed to ``True`` (require attribution) for a non-boolean value."""
        if isinstance(value, bool):
            return value
        _warn_fail_closed(
            "attribution_required", value, "True", reason="is not a boolean"
        )
        return True


# ---- Medium contracts (FEAT-041 §5) ----

CitationNorm = Literal["every-claim", "light", "none"]
"""How strictly a medium requires claims to be cited."""


class MediumContract(BaseModel):
    """A medium contract loaded from a ``<medium>.MEDIUM.md`` skill file (FEAT-041 §5).

    The Conductor loads one of these to drive structure, specialist weighting,
    the citation norm, the default privacy tier, and the reflection rubric for
    an authoring run.

    Attributes:
        medium: The medium slug (e.g. ``research``).
        structure: Ordered section names the draft should follow.
        specialist_weights: Relative weight per specialist (``graph``,
            ``retrieval``, ``ontology``, ``voice``); higher means more
            influence on the synthesized draft.
        citation_norm: How strictly claims must be cited.
        default_privacy_tier: The privacy tier a draft defaults to.
        reflection_rubric: Per-dimension weights (§8) the reflection node
            scores against — e.g. ``ontological_accuracy``,
            ``citation_completeness``, ``voice_fidelity``.
    """

    model_config = ConfigDict(use_enum_values=True, extra="ignore")

    medium: str
    structure: list[str] = Field(default_factory=list)
    specialist_weights: dict[str, float] = Field(default_factory=dict)
    citation_norm: CitationNorm = "light"
    default_privacy_tier: PrivacyTier = PrivacyTier.OPEN
    reflection_rubric: dict[str, float] = Field(default_factory=dict)


# ---- Nested Models ----


class FragmentSource(BaseModel):
    """Source metadata for a fragment, describing where it was ingested from."""

    model_config = ConfigDict(use_enum_values=True)

    platform: SourcePlatform
    kind: SourceKind = SourceKind.UNCLASSIFIED
    original_file: str | None = None
    original_encoding: str | None = None
    conversation_id: str | None = None
    channel: str | None = None
    interlocutor: str | None = None
    author: Authorship = Authorship.SELF
    # FEAT-041 §7.4: the `11-Other-Authors/<slug>/` folder this fragment came
    # from, or ``None`` for a native self/owner fragment. Distinct from
    # ``author`` (the self|ai|other|collaborative axis), which is unchanged.
    author_slug: str | None = None

    @field_validator("author_slug")
    @classmethod
    def _reject_unsafe_author_slug(cls, value: str | None) -> str | None:
        """Reject an ``author_slug`` that is not a single safe path segment.

        ``author_slug`` names an ``11-Other-Authors/<slug>/`` folder and is
        interpolated into vault paths by the writer and the manifest loader, so
        a value carrying a path separator or a ``.``/``..`` traversal token
        could escape the vault. Such a value is rejected at the data boundary
        (#470). ``None`` (a native fragment) is always valid; the vault reader
        skips any on-disk fragment whose frontmatter fails this check.
        """
        if value is None:
            return None
        if (
            not value
            or value in {".", ".."}
            or "/" in value
            or "\\" in value
            or "\x00" in value
            or any(ch.isspace() for ch in value)
        ):
            msg = f"author_slug must be a single safe path segment, got {value!r}"
            raise ValueError(msg)
        return value


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

    Attributes:
        voice_weight: How much this fragment may influence generated voice,
            in ``[0, 1]``. ``1.0`` (the default) is a native, full-weight
            self-authored fragment; a borrowed fragment is weighted lower.
            Fails closed to ``1.0`` on a malformed value (FEAT-041 §7.4).
        representativeness: How closely the fragment stands for the owner's
            own views — ``self`` / ``endorsed`` / ``aspirational`` /
            ``reference``. Defaults to ``self``; fails closed to ``self``.
        source: Source metadata. Its ``author_slug`` names the
            ``11-Other-Authors/<slug>/`` folder a borrowed fragment came from
            (``None`` for a native fragment), distinct from ``source.author``.
    """

    model_config = ConfigDict(use_enum_values=True)

    type: str = "fragment"
    id: str
    title: str
    source: FragmentSource
    # Two-axis attribution (FEAT-041 §7.4). ``voice_weight`` is how much this
    # fragment may influence generated voice (1.0 = full weight);
    # ``representativeness`` is how closely it stands for the owner's own views.
    # The defaults make every pre-FEAT-041 fragment behave as a full-weight,
    # self-authored fragment — a purely additive, backward-compatible change.
    # Both fail closed to those native defaults so a hand-corrupted fragment
    # never crashes a vault scan (matching :class:`AuthorManifest`).
    voice_weight: float = 1.0
    representativeness: Representativeness = "self"
    created: datetime = Field(default_factory=now_la)
    ingested: datetime = Field(default_factory=now_la)
    # Timestamp the source itself records (a Substack post's publish
    # date, a Discord message's ``timestamp``, …); ``None`` is the
    # honest answer when no source date is extractable — never guess.
    authored_at: datetime | None = None
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
    # FEAT-020 hierarchy; defaults (None, [], "document", []) = root document.
    parent_id: str | None = None
    child_ids: list[str] = Field(default_factory=list)
    level: FragmentLevel = "document"
    structural_path: list[str] = Field(default_factory=list)
    # Optional weighted-ontology profile parallel to the legacy
    # ``frequency`` / ``wavelength`` / ``voice`` fields. ``None``
    # means "no weighted detection ran" (legacy vaults). The forward
    # reference is resolved at the bottom of this module via a late
    # import of :mod:`creek.classify.weighted` so the circular-import
    # risk between models and the classify package stays contained.
    weighted: "WeightedFragmentClassification | None" = None

    @field_validator("voice_weight", mode="before")
    @classmethod
    def _coerce_voice_weight(cls, value: object) -> float:
        """Fail closed to the native 1.0 weight for a bad ``voice_weight``."""
        weight = _safe_float(value)
        if weight is None or not 0.0 <= weight <= 1.0:
            _warn_fail_closed(
                "Fragment voice_weight",
                value,
                "1.0",
                reason="is not a number in [0, 1]",
            )
            return 1.0
        return weight

    @field_validator("representativeness", mode="before")
    @classmethod
    def _coerce_representativeness(cls, value: object) -> str:
        """Fail closed to ``self`` for an unrecognised ``representativeness``."""
        if isinstance(value, str) and value in _REPRESENTATIVENESS:
            return value
        _warn_fail_closed("Fragment representativeness", value, "self")
        return "self"

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
        level_a: Structural level of the earlier fragment (FEAT-024).
            Defaults to ``"document"`` for flat-fragment vaults.
        level_b: Structural level of the later fragment (FEAT-024).
            When ``level_a != level_b`` the pair is *cross-level*, which
            ranks above same-level pairs in
            :class:`~creek.generate.synchronicity.SynchronicityDetector`.
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
    level_a: FragmentLevel = "document"
    level_b: FragmentLevel = "document"
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
        level_policy: FEAT-025 level policy used to filter source
            fragments. ``"leaves"`` is the default — parents serve as
            ``structural_path`` context rather than duplicate content.
            ``"all"`` reproduces the pre-FEAT-025 behaviour and is
            recorded explicitly for re-compile audits.
        source_levels: Sorted distinct ``Fragment.level`` values of the
            fragments that actually fed the synthesis after the policy
            filter ran. Empty on pre-FEAT-025 pages.
    """

    model_config = ConfigDict(use_enum_values=True)

    type: Literal["compiled_page"] = "compiled_page"
    """Discriminator field. Reserved for future ``Annotated[Union[...],
    Field(discriminator="type")]`` dispatch when the compiled layer
    grows beyond ``CompiledPage`` — pinning the literal here means
    mypy will narrow correctly once the union exists.
    """
    target_kind: CompileTargetKind
    target_id: str
    title: str
    body: str = ""
    provenance: list[ProvenanceEntry] = Field(default_factory=list)
    compiled_at: datetime = Field(default_factory=_utc_now)
    compile_method: CompileMethod = "llm"
    level_policy: str = "leaves"
    source_levels: list[str] = Field(default_factory=list)


# Late import + ``Fragment.model_rebuild`` to resolve the
# :attr:`Fragment.weighted` forward reference. ``weighted.py`` needs
# Fragment and the enum types from this module, so it must load AFTER
# them; that puts our half of the cycle here at module bottom. The
# ``noqa: E402`` below marks the deliberate placement; a top-level
# import would fail
# with ``ImportError``.
from creek.classify.weighted import (  # noqa: E402  # wrong-import-position
    WeightedFragmentClassification as _WeightedFragmentClassification,
)

Fragment.model_rebuild(
    _types_namespace={
        "WeightedFragmentClassification": _WeightedFragmentClassification,
    },
)
