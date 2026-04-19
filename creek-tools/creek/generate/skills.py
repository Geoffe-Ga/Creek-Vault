"""Voice Skill Tree generation.

Implements Section 11.4 of the Creek Ontology: a tree of ``SKILL.md``
files that teach an LLM how to write in the human's voice for any
combination of frequency, phase, mode/orientation, register, thread, or
eddy. Each skill follows the Claude Code skill format — activation
section, description, exemplar passages, writing instructions,
anti-patterns, and combination guidance.

The :class:`SkillTreeGenerator` reads Fragment markdown files from the
vault to harvest exemplar passages; ontology constants (frequency
themes, phase rhythms, mode stances, register descriptions) are baked
into this module so the generator can produce useful output even from a
sparsely-populated vault.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, TypeVar

import frontmatter
import yaml
from pydantic import BaseModel, ValidationError

from creek.generate.indexes import (
    FREQUENCY_NAMES,
    FREQUENCY_SIGNALS,
    FREQUENCY_THEMES,
)
from creek.models import (
    Eddy,
    Fragment,
    Frequency,
    Mode,
    Orientation,
    Phase,
    PrivacyTier,
    Thread,
    VoiceRegister,
)

if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path

logger = logging.getLogger(__name__)

_ModelT = TypeVar("_ModelT", Thread, Eddy)
_AnyModelT = TypeVar("_AnyModelT", bound=BaseModel)

# ---- Canonical ontology constants ----

FREQUENCY_KEYS: tuple[str, ...] = tuple(
    freq.value for freq in Frequency if freq is not Frequency.UNCLASSIFIED
)
"""All ten APTITUDE frequency keys (F1-F10)."""

PHASE_KEYS: tuple[str, ...] = tuple(
    phase.value for phase in Phase if phase is not Phase.UNCLASSIFIED
)
"""The six Archetypal Wavelength phases."""

REGISTER_KEYS: tuple[str, ...] = tuple(reg.value for reg in VoiceRegister)
"""The seven canonical voice registers."""

MODE_ORIENTATION_KEYS: tuple[tuple[str, str], ...] = (
    ("inhabit", "do"),
    ("inhabit", "feel"),
    ("express", "do"),
    ("express", "feel"),
    ("collaborate", "do"),
    ("collaborate", "feel"),
    ("integrate", "do"),
    ("integrate", "feel"),
    ("absorb", "do_feel"),
)
"""The nine Mode/Orientation pairs defined by the Creek ontology."""

DEFAULT_MIN_THREAD_FRAGMENTS: int = 10
"""Threads must have more than this many fragments to earn a skill."""

DEFAULT_MIN_EDDY_FRAGMENTS: int = 15
"""Eddies must have more than this many fragments to earn a skill."""

DEFAULT_MAX_EXEMPLARS: int = 5
"""Maximum exemplar passages included in a single skill file."""

DEFAULT_MIN_EXEMPLARS: int = 3
"""Target minimum exemplar passages per skill."""

_FRAGMENTS_SUBDIR: str = "01-Fragments"
_THREADS_SUBDIR: str = "02-Threads"
_EDDIES_SUBDIR: str = "03-Eddies"

_FREQUENCIES_DIR: str = "frequencies"
_PHASES_DIR: str = "phases"
_MODES_DIR: str = "modes"
_REGISTERS_DIR: str = "registers"
_THREADS_DIR: str = "threads"
_EDDIES_DIR: str = "eddies"
_META_DIR: str = "meta"

_SKILL_SUFFIX: str = ".SKILL.md"

_EXEMPLAR_WORDS_MIN: int = 30
"""Exemplar passages below this word count are skipped as too thin."""

_EXEMPLAR_WORDS_MAX: int = 180
"""Exemplar passages longer than this are truncated at a sentence boundary."""


# ---- Frequency voice descriptors ----

FREQUENCY_VOICE_TEXTURES: dict[str, str] = {
    "F1": (
        "Short declarative sentences that drive toward action. "
        "Metaphors of building, running, hauling. Verbs carry the weight. "
        "First-person singular present tense. No hedging."
    ),
    "F2": (
        "Slow cadence with pauses built into the prose. "
        "Metaphors of water, tide, breath, kinship. "
        "The voice receives before it claims. "
        "Sentences end in reflection rather than conclusion."
    ),
    "F3": (
        "Declarative first-person with heat behind it. "
        "Metaphors of fire, edge, spine, standing ground. "
        "The voice owns its authority without apology, "
        "yet returns to self-compassion when the heat threatens to scorch."
    ),
    "F4": (
        "Collective-first phrasing — 'we', 'us', 'the work'. "
        "Metaphors of path, vow, lineage, discipline. "
        "Cadence is liturgical: recurrent rhythm, purposive closure."
    ),
    "F5": (
        "Precise, qualified claims stacked toward synthesis. "
        "Metaphors illustrate but do not carry the argument. "
        "Sentence structure reflects logical structure — "
        "clauses map to premises, paragraphs to inferences."
    ),
    "F6": (
        "Warm, embodied second-person moments break through first-person reflection. "
        "Metaphors of thread, skin, weaving, meeting. "
        "Sentences soften at the edges. "
        "The voice lets feeling shape syntax."
    ),
    "F7": (
        "Long compound sentences that hold multiple frames at once. "
        "Metaphors of lattice, map, current, ecosystem. "
        "Parentheticals and em-dashes braid related patterns. "
        "The voice refuses to flatten nuance."
    ),
    "F8": (
        "Quiet declarative sentences that arrive from stillness. "
        "Metaphors of listening, alignment, the thread that pulls. "
        "First-person singular but oriented toward a larger self. "
        "No urgency, no argument — recognition."
    ),
    "F9": (
        "Spare, luminous sentences. "
        "Metaphors of light, silence, open sky. "
        "Subject and object dissolve mid-paragraph. "
        "Cadence breathes; the voice lets the claim be small."
    ),
    "F10": (
        "Plain sentences without ornament. "
        "Metaphors of empty hands, open palms, a glass set down. "
        "The voice smiles at its own insistence. "
        "Paragraphs end in deflation rather than crescendo."
    ),
}
"""How each frequency sounds in the human's voice — texture guidance."""


FREQUENCY_MEDICINE_VS_TOXIC: dict[str, tuple[str, str]] = {
    "F1": (
        "Steady, sequenced, builds from the next concrete step.",
        "Frantic, grasping, stacks commitments without breath.",
    ),
    "F2": (
        "Receptive, slow, lets the image arrive before naming it.",
        "Ungrounded grandiosity or anxious self-doubt.",
    ),
    "F3": (
        "Confident without cruelty; owns power while leaving room for others.",
        "Dominating, shaming, or collapsing into self-loathing.",
    ),
    "F4": (
        "Devotional, clear about shared purpose, honours the vow.",
        "Rigid moralism, score-keeping, repression of dissent.",
    ),
    "F5": (
        "Curious, evidence-led, open to the result overturning the hypothesis.",
        "Force-fitting the data, crusading, presuming the conclusion.",
    ),
    "F6": (
        "Vulnerable, attuned, lets contact shape the sentence.",
        "Oversharing, enmeshment, or bitter withdrawal.",
    ),
    "F7": (
        "Synthesises across frames without collapsing any of them.",
        "Abstract to the point of vapour; refuses to land.",
    ),
    "F8": (
        "Listens before speaking; acts from alignment rather than ego.",
        "Mistakes pattern-seeking for gnosis; spiritual bypass.",
    ),
    "F9": (
        "Small, luminous, no claim beyond what the moment holds.",
        "Grandiose or dissociated; ego borrowing the language of unity.",
    ),
    "F10": (
        "Light, spacious, humour about the whole project.",
        "Nihilism dressed as wisdom; refusal to care.",
    ),
}
"""Per-frequency medicine vs toxic prose signatures."""


# ---- Phase voice descriptors ----

PHASE_VOICE_RHYTHMS: dict[str, str] = {
    "rising": (
        "Energy is gathering. Sentences lengthen as confidence builds. "
        "Topics expand outward from self to project to world. "
        "The voice is willing to commit in ink."
    ),
    "peaking": (
        "Energy is at full height. Declarative, generative, prolific. "
        "Paragraphs synthesise; sentences land with authority. "
        "Risk is the voice claiming more than it has lived."
    ),
    "withdrawal": (
        "Energy is receding by choice. Sentences shorten. "
        "The voice turns inward — reflection, recalibration, careful honesty. "
        "Metaphors shift from fire to water."
    ),
    "diminishing": (
        "Energy is leaking. The voice notices its own depletion. "
        "Short paragraphs, honest admissions, questions more than claims. "
        "The work is to name what is losing colour without dramatising."
    ),
    "bottoming_out": (
        "Energy is at its lowest. The voice is raw, unguarded, sometimes fragmented. "
        "Short lines. Long white space between them. "
        "Truth-telling without the strength to shape it."
    ),
    "restoration": (
        "Energy is returning quietly. Sentences regain rhythm without force. "
        "Topics narrow to small, concrete acts of repair. "
        "The voice is grateful without being effusive."
    ),
}
"""How writing energy, sentence structure, and topics shift by wavelength phase."""


# ---- Mode/Orientation voice stances ----

MODE_ORIENTATION_STANCES: dict[tuple[str, str], str] = {
    ("inhabit", "do"): (
        "Writing from inside a frequency while taking action in it. "
        "First-person present tense, verbs in motion, minimal distance "
        "between the self and the work."
    ),
    ("inhabit", "feel"): (
        "Writing from inside a frequency while feeling it. "
        "Sensory detail, somatic anchoring, emotional specificity. "
        "The voice names what is alive in the body."
    ),
    ("express", "do"): (
        "Writing to enact a frequency through action visible to others. "
        "Imperative and declarative moods. "
        "The reader is addressed as participant or witness."
    ),
    ("express", "feel"): (
        "Writing to enact a frequency through feeling shared with others. "
        "Confessional cadence, invitation to accompany, "
        "the emotional register made legible on the page."
    ),
    ("collaborate", "do"): (
        "Writing alongside others doing the work. "
        "First-person plural, task-oriented, honest about disagreement. "
        "The voice credits collaborators without losing its own stance."
    ),
    ("collaborate", "feel"): (
        "Writing alongside others in shared feeling. "
        "Slow, attuned, willing to be changed by the exchange. "
        "The voice leaves room on the page for the other."
    ),
    ("integrate", "do"): (
        "Writing to weave past action into present coherence. "
        "Retrospective cadence, sequenced lessons, honest accounting. "
        "The voice metabolises rather than reports."
    ),
    ("integrate", "feel"): (
        "Writing to weave past feeling into present coherence. "
        "Memoir-inflected, patient with contradiction, "
        "willing to hold joy and grief in the same paragraph."
    ),
    ("absorb", "do_feel"): (
        "Writing from a posture of receiving. "
        "Quiet, observational, first-person but peripheral. "
        "The voice listens before it speaks and often stops before it concludes."
    ),
}
"""The functional stance of writing in each Mode/Orientation combination."""


# ---- Register voice descriptors ----

REGISTER_VOICE_PROMPTS: dict[str, str] = {
    "confessional": (
        "Write in a confessional register. Use first person. "
        "Start from vulnerability and sensory detail, then arrive at insight. "
        "Let paragraphs build from concrete experience to abstract reflection. "
        "Favour em-dashes for asides."
    ),
    "analytical": (
        "Write in an analytical register. Use precise, qualified claims "
        "grounded in evidence. Move from observation to inference to "
        "implication. Metaphors illustrate; they do not carry the argument."
    ),
    "playful": (
        "Write in a playful register. Let wit carry the argument. "
        "Puns, reversals, and small jokes are welcome. "
        "Irony underlines sincerity rather than distancing from it."
    ),
    "prophetic": (
        "Write in a prophetic register. Speak with declarative clarity. "
        "Let each claim stand without apology. "
        "Cadence matters — short lines have weight."
    ),
    "instructional": (
        "Write in an instructional register. Lead the reader one step at a time. "
        "Define every term on first use. "
        "Resolve each paragraph to an actionable understanding."
    ),
    "raw": (
        "Write in a raw register. Preserve the rough edges of the thought. "
        "Let ideas arrive half-formed. Use fragments. "
        "Trust the reader to fill the gaps."
    ),
    "conversational": (
        "Write in a conversational register. Address the reader directly. "
        "Use the second person when natural. "
        "Keep paragraphs short so the exchange stays alive."
    ),
}
"""Full voice prompt templates per register (Section 11.2)."""


REGISTER_ANTI_PATTERNS: dict[str, tuple[str, ...]] = {
    "confessional": (
        "Do not use corporate or management language.",
        "Do not hedge with qualifiers like 'perhaps' or 'possibly'.",
        "Do not structure thoughts as bullet points.",
    ),
    "analytical": (
        "Do not assert emotion without evidence.",
        "Do not adopt confessional first-person intimacy.",
        "Do not issue prophetic pronouncements.",
    ),
    "playful": (
        "Do not adopt formal academic solemnity.",
        "Do not use jargon without winking at it.",
        "Do not chain flat declarative sentences without variation.",
    ),
    "prophetic": (
        "Do not hedge or qualify the vision.",
        "Do not soften the claim with bureaucratic disclaimers.",
        "Do not diminish the statement with self-deprecation.",
    ),
    "instructional": (
        "Do not insert confessional asides that detour the reader.",
        "Do not leave jargon undefined on first use.",
        "Do not stop at metaphor without making the mechanism explicit.",
    ),
    "raw": (
        "Do not polish the rough edges into smoothness.",
        "Do not pre-empt the reader with explanatory framing.",
        "Do not translate the moment into academic language.",
    ),
    "conversational": (
        "Do not monologue without addressing the reader.",
        "Do not use academic distance or formal register.",
        "Do not bury the exchange under dense paragraphs.",
    ),
}
"""Per-register constraints the voice should never violate."""


# ---- Data classes ----


@dataclass(frozen=True)
class SkillExemplar:
    """A short voice exemplar harvested from a Fragment for inclusion in a SKILL."""

    fragment_id: str
    fragment_title: str
    passage: str


@dataclass(frozen=True)
class VaultSnapshot:
    """In-memory snapshot of qualifying fragments, threads, and eddies."""

    fragments: tuple[tuple[Fragment, str], ...]
    threads: tuple[Thread, ...]
    eddies: tuple[Eddy, ...]


# ---- Loaders ----


def _safe_load_post(md_file: Path, *, label: str) -> frontmatter.Post | None:
    """Load a frontmatter ``Post`` from *md_file*, logging on failure."""
    try:
        return frontmatter.load(str(md_file))
    except (OSError, ValueError, yaml.YAMLError):
        logger.debug("Skipping unreadable %s: %s", label, md_file)
        return None


def _safe_validate(
    model_cls: type[_AnyModelT],
    metadata: dict[str, object],
    *,
    label: str,
    path: Path,
) -> _AnyModelT | None:
    """Validate *metadata* against *model_cls*, logging on failure."""
    try:
        return model_cls.model_validate(metadata)
    except ValidationError:
        logger.debug("Skipping invalid %s frontmatter: %s", label, path)
        return None


def _load_fragment(md_file: Path) -> tuple[Fragment, str] | None:
    """Parse a fragment markdown file and return ``(fragment, body)``.

    Args:
        md_file: Path to the fragment markdown file.

    Returns:
        ``(fragment, body)`` when the file is a valid fragment record, or
        ``None`` when it cannot be loaded or is not a fragment.
    """
    post = _safe_load_post(md_file, label="fragment")
    if post is None:
        return None
    metadata = dict(post.metadata)
    if metadata.get("type") != "fragment":
        return None
    fragment = _safe_validate(Fragment, metadata, label="fragment", path=md_file)
    if fragment is None:
        return None
    return fragment, post.content


def _load_typed_model(
    md_file: Path,
    *,
    expected_type: str,
    model_cls: type[_ModelT],
) -> _ModelT | None:
    """Parse a ``Thread`` or ``Eddy`` markdown file into its model.

    Args:
        md_file: Path to the markdown file.
        expected_type: The value required under the frontmatter ``type``
            field (``"thread"`` or ``"eddy"``).
        model_cls: The model class to validate against.

    Returns:
        A validated model instance, or ``None`` when the file cannot be
        loaded, does not match the expected type, or fails validation.
    """
    post = _safe_load_post(md_file, label=expected_type)
    if post is None:
        return None
    metadata = dict(post.metadata)
    if metadata.get("type") != expected_type:
        return None
    return _safe_validate(model_cls, metadata, label=expected_type, path=md_file)


def _is_snapshot_fragment(fragment: Fragment, *, allow_intimate: bool) -> bool:
    """Return ``True`` when *fragment* is eligible for the snapshot."""
    if not fragment.voice_proxy_eligible:
        return False
    is_intimate = str(fragment.privacy_tier) == PrivacyTier.INTIMATE.value
    return allow_intimate or not is_intimate


def _collect_fragments(
    fragments_root: Path, *, allow_intimate: bool
) -> list[tuple[Fragment, str]]:
    """Load proxy-eligible fragments under *fragments_root*."""
    if not fragments_root.exists():
        return []
    collected: list[tuple[Fragment, str]] = []
    for md_file in sorted(fragments_root.rglob("*.md")):
        loaded = _load_fragment(md_file)
        if loaded is None:
            continue
        fragment, body = loaded
        if not _is_snapshot_fragment(fragment, allow_intimate=allow_intimate):
            continue
        collected.append((fragment, body))
    return collected


def _collect_typed(
    root: Path, *, expected_type: str, model_cls: type[_ModelT]
) -> list[_ModelT]:
    """Load validated *model_cls* records from markdown files under *root*."""
    if not root.exists():
        return []
    collected: list[_ModelT] = []
    for md_file in sorted(root.rglob("*.md")):
        model = _load_typed_model(
            md_file, expected_type=expected_type, model_cls=model_cls
        )
        if isinstance(model, model_cls):
            collected.append(model)
    return collected


def _load_vault_snapshot(
    vault_path: Path,
    *,
    allow_intimate: bool,
) -> VaultSnapshot:
    """Scan *vault_path* for fragments, threads, and eddies.

    Args:
        vault_path: Root of the Obsidian vault.
        allow_intimate: When ``False``, fragments tagged ``intimate`` are
            excluded from the snapshot to preserve privacy boundaries.

    Returns:
        A :class:`VaultSnapshot` containing validated records.
    """
    fragments = _collect_fragments(
        vault_path / _FRAGMENTS_SUBDIR, allow_intimate=allow_intimate
    )
    threads = _collect_typed(
        vault_path / _THREADS_SUBDIR, expected_type="thread", model_cls=Thread
    )
    eddies = _collect_typed(
        vault_path / _EDDIES_SUBDIR, expected_type="eddy", model_cls=Eddy
    )
    return VaultSnapshot(
        fragments=tuple(fragments),
        threads=tuple(threads),
        eddies=tuple(eddies),
    )


# ---- Exemplar harvesting ----

_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")


def _extract_passage(body: str) -> str | None:
    """Return a concise exemplar passage from *body*.

    The passage is the first contiguous run of sentences whose total
    word count falls between :data:`_EXEMPLAR_WORDS_MIN` and
    :data:`_EXEMPLAR_WORDS_MAX`. Sentence boundaries are respected:
    sentences are never split, so a passage that would exceed the max
    by including the next sentence stops at the previous sentence
    boundary. If even the first sentence alone exceeds the max, no
    passage is produced (the function returns ``None``). Also returns
    ``None`` when the body is empty or composed entirely of
    over-length sentences.
    """
    text = body.strip()
    if not text:
        return None
    sentences = [s.strip() for s in _SENTENCE_SPLIT_RE.split(text) if s.strip()]
    buf: list[str] = []
    word_count = 0
    for sentence in sentences:
        words = sentence.split()
        if word_count + len(words) > _EXEMPLAR_WORDS_MAX:
            break
        buf.append(sentence)
        word_count += len(words)
        if word_count >= _EXEMPLAR_WORDS_MIN:
            break
    if word_count < _EXEMPLAR_WORDS_MIN:
        return None
    return " ".join(buf)


def _build_exemplar(
    fragment: Fragment,
    body: str,
) -> SkillExemplar | None:
    """Build a :class:`SkillExemplar` from a fragment if one can be extracted."""
    passage = _extract_passage(body)
    if passage is None:
        return None
    return SkillExemplar(
        fragment_id=fragment.id,
        fragment_title=fragment.title,
        passage=passage,
    )


def _slugify(text: str) -> str:
    """Convert *text* to a filesystem-safe slug.

    Returns ``"untitled"`` when *text* has no alphanumeric content.
    """
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug or "untitled"


def _unique_slug(base: str, fallback_id: str, used: set[str]) -> str:
    """Return *base* or a disambiguated slug when it collides.

    Tries *base* first, then ``{base}-{slugified-id}``, then appends a
    monotonically-increasing counter until the candidate is absent from
    *used*. Guarantees no silent overwrite even when ids themselves
    slugify to the same string.
    """
    if base not in used:
        return base
    disambiguated = f"{base}-{_slugify(fallback_id)}"
    if disambiguated not in used:
        return disambiguated
    counter = 2
    while f"{disambiguated}-{counter}" in used:
        counter += 1
    return f"{disambiguated}-{counter}"


def _mode_orientation_key(mode: str, orientation: str) -> str:
    """Compose a Mode/Orientation filesystem key like ``inhabit-do``."""
    normalised_orientation = orientation.replace("_", "-")
    return f"{mode}-{normalised_orientation}"


# ---- Combination guidance ----

_FREQUENCY_PHASE_HINTS: dict[str, str] = {
    "F1": (
        "When F1 meets Withdrawal, the drive cools into planning. "
        "Metaphors shift from running to sorting."
    ),
    "F3": (
        "When F3 meets Withdrawal, the voice shifts from confidence to "
        "vulnerable honesty; sentences shorten, metaphors move from fire to water."
    ),
    "F5": (
        "When F5 meets Bottoming Out, the analytical scaffolding collapses "
        "into raw questions; evidence gives way to unknowing."
    ),
    "F6": (
        "When F6 meets Peaking, warmth becomes infectious; second-person "
        "moments multiply and sentences open outward."
    ),
    "F8": (
        "When F8 meets Restoration, alignment manifests as small, "
        "grateful acts; the voice refuses grandeur."
    ),
}
"""Specific combination hints per frequency, keyed for explicit pairings."""

_FREQUENCY_REGISTER_HINTS: dict[str, str] = {
    "F2": (
        "With the Confessional register, F2 becomes a slow ceremonial unveiling "
        "— the voice receives the insight as gift rather than conquest."
    ),
    "F3": (
        "With the Prophetic register, F3 heat condenses into declarative clarity; "
        "with the Raw register, F3 anger keeps its teeth on the page."
    ),
    "F7": (
        "With the Analytical register, F7 becomes a synthesising map-maker; "
        "with the Playful register, F7 juggles frames with self-aware wit."
    ),
    "F9": (
        "With the Raw register, F9 refuses to dress mystical experience in "
        "borrowed robes; with the Confessional register, it stays bodily "
        "and specific."
    ),
}
"""Specific combination hints per frequency, keyed by register interaction."""


# ---- Rendering helpers ----


def _render_section(title: str, body: str) -> str:
    """Render a ``## Title`` section followed by body text, trimmed."""
    return f"## {title}\n\n{body.strip()}\n"


def _render_bullet_list(items: Iterable[str]) -> str:
    """Render *items* as a markdown bullet list."""
    return "\n".join(f"- {item}" for item in items)


def _render_exemplar_section(exemplars: list[SkillExemplar]) -> str:
    """Render exemplar passages as a ``## Exemplar Passages`` section."""
    if not exemplars:
        body = (
            "_No qualifying exemplars were found in the vault. Add more "
            "voice-classified Fragments to populate this section._"
        )
        return _render_section("Exemplar Passages", body)
    lines: list[str] = []
    for exemplar in exemplars:
        lines.extend(
            (
                f"> **{exemplar.fragment_title}** (`{exemplar.fragment_id}`)",
                ">",
            ),
        )
        for paragraph_line in exemplar.passage.splitlines() or [exemplar.passage]:
            lines.append(f"> {paragraph_line}")
        lines.append("")
    return _render_section("Exemplar Passages", "\n".join(lines).rstrip())


def _write_skill(
    target: Path,
    *,
    category: str,
    key: str,
    title: str,
    body: str,
    extra_tags: Iterable[str] = (),
) -> Path:
    """Persist a SKILL.md file with frontmatter and return its path."""
    target.parent.mkdir(parents=True, exist_ok=True)
    tags = ["skill", category, *extra_tags]
    post = frontmatter.Post(
        content=body.strip() + "\n",
        type="skill",
        category=category,
        key=key,
        title=title,
        generated_date=datetime.now(tz=UTC).isoformat(),
        tags=tags,
    )
    target.write_text(frontmatter.dumps(post), encoding="utf-8")
    return target


# ---- Generator ----


class SkillTreeGenerator:
    """Generate the tree of ``SKILL.md`` files from the vault.

    Attributes:
        min_thread_fragments: Threads must have more fragments than this
            to earn a skill file. Defaults to ten.
        min_eddy_fragments: Eddies must have more fragments than this to
            earn a skill file. Defaults to fifteen.
        max_exemplars: Maximum exemplar passages included in each skill
            file. Defaults to five.
        allow_intimate: When ``True``, fragments tagged ``intimate``
            participate in exemplar harvesting. Defaults to ``False``.
    """

    def __init__(
        self,
        *,
        min_thread_fragments: int = DEFAULT_MIN_THREAD_FRAGMENTS,
        min_eddy_fragments: int = DEFAULT_MIN_EDDY_FRAGMENTS,
        max_exemplars: int = DEFAULT_MAX_EXEMPLARS,
        allow_intimate: bool = False,
    ) -> None:
        """Initialise the generator with the given configuration.

        Args:
            min_thread_fragments: Minimum ``fragment_count`` required
                for a thread to earn a skill (strict >).
            min_eddy_fragments: Minimum ``fragment_count`` required for
                an eddy to earn a skill (strict >).
            max_exemplars: Maximum exemplar passages per skill.
            allow_intimate: Whether to include ``intimate`` privacy
                tier fragments during harvesting.

        Raises:
            ValueError: When any numeric threshold is negative.
        """
        if min_thread_fragments < 0:
            msg = "min_thread_fragments must be non-negative"
            raise ValueError(msg)
        if min_eddy_fragments < 0:
            msg = "min_eddy_fragments must be non-negative"
            raise ValueError(msg)
        if max_exemplars < 1:
            msg = "max_exemplars must be at least 1"
            raise ValueError(msg)
        self.min_thread_fragments = min_thread_fragments
        self.min_eddy_fragments = min_eddy_fragments
        self.max_exemplars = max_exemplars
        self.allow_intimate = allow_intimate

    # -- Public surface ------------------------------------------------

    def generate_all_skills(
        self,
        vault_path: Path,
        output_dir: Path,
    ) -> list[Path]:
        """Generate every category of skill file.

        Args:
            vault_path: Root of the Obsidian vault to read from.
            output_dir: Destination directory for the skill tree.

        Returns:
            All written SKILL file paths, in category order.
        """
        snapshot = _load_vault_snapshot(
            vault_path,
            allow_intimate=self.allow_intimate,
        )
        written: list[Path] = []
        written.extend(self._generate_frequency_skills(snapshot, output_dir))
        written.extend(self._generate_phase_skills(snapshot, output_dir))
        written.extend(self._generate_mode_skills(snapshot, output_dir))
        written.extend(self._generate_register_skills(snapshot, output_dir))
        written.extend(self._generate_thread_skills(snapshot, output_dir))
        written.extend(self._generate_eddy_skills(snapshot, output_dir))
        written.extend(self.generate_meta_skills(output_dir))
        return written

    def generate_frequency_skills(
        self,
        vault_path: Path,
        output_dir: Path,
        snapshot: VaultSnapshot | None = None,
    ) -> list[Path]:
        """Generate one SKILL.md per APTITUDE frequency (F1-F10).

        Pass a pre-loaded *snapshot* to skip the vault scan when this method
        is invoked alongside other per-category generators.
        """
        resolved = self._resolve_snapshot(vault_path, snapshot)
        return self._generate_frequency_skills(resolved, output_dir)

    def generate_phase_skills(
        self,
        vault_path: Path,
        output_dir: Path,
        snapshot: VaultSnapshot | None = None,
    ) -> list[Path]:
        """Generate one SKILL.md per Archetypal Wavelength phase.

        Pass a pre-loaded *snapshot* to skip the vault scan when this method
        is invoked alongside other per-category generators.
        """
        resolved = self._resolve_snapshot(vault_path, snapshot)
        return self._generate_phase_skills(resolved, output_dir)

    def generate_mode_skills(
        self,
        vault_path: Path,
        output_dir: Path,
        snapshot: VaultSnapshot | None = None,
    ) -> list[Path]:
        """Generate one SKILL.md per Mode/Orientation pair (nine total).

        Pass a pre-loaded *snapshot* to skip the vault scan when this method
        is invoked alongside other per-category generators.
        """
        resolved = self._resolve_snapshot(vault_path, snapshot)
        return self._generate_mode_skills(resolved, output_dir)

    def generate_register_skills(
        self,
        vault_path: Path,
        output_dir: Path,
        snapshot: VaultSnapshot | None = None,
    ) -> list[Path]:
        """Generate one SKILL.md per voice register (seven total).

        Pass a pre-loaded *snapshot* to skip the vault scan when this method
        is invoked alongside other per-category generators.
        """
        resolved = self._resolve_snapshot(vault_path, snapshot)
        return self._generate_register_skills(resolved, output_dir)

    def generate_thread_skills(
        self,
        vault_path: Path,
        output_dir: Path,
        snapshot: VaultSnapshot | None = None,
    ) -> list[Path]:
        """Generate one SKILL.md per qualifying thread.

        Pass a pre-loaded *snapshot* to skip the vault scan when this method
        is invoked alongside other per-category generators.
        """
        resolved = self._resolve_snapshot(vault_path, snapshot)
        return self._generate_thread_skills(resolved, output_dir)

    def generate_eddy_skills(
        self,
        vault_path: Path,
        output_dir: Path,
        snapshot: VaultSnapshot | None = None,
    ) -> list[Path]:
        """Generate one SKILL.md per qualifying eddy.

        Pass a pre-loaded *snapshot* to skip the vault scan when this method
        is invoked alongside other per-category generators.
        """
        resolved = self._resolve_snapshot(vault_path, snapshot)
        return self._generate_eddy_skills(resolved, output_dir)

    def _resolve_snapshot(
        self,
        vault_path: Path,
        snapshot: VaultSnapshot | None,
    ) -> VaultSnapshot:
        """Return *snapshot* if provided, else scan *vault_path* once."""
        if snapshot is not None:
            return snapshot
        return _load_vault_snapshot(
            vault_path,
            allow_intimate=self.allow_intimate,
        )

    def generate_meta_skills(self, output_dir: Path) -> list[Path]:
        """Generate the two meta skills (voice-core and activation guide).

        Args:
            output_dir: Destination directory for the skill tree.

        Returns:
            Paths to the two written meta SKILL files.
        """
        meta_dir = output_dir / _META_DIR
        written: list[Path] = [
            _write_skill(
                meta_dir / f"voice-core{_SKILL_SUFFIX}",
                category="meta",
                key="voice-core",
                title="Voice Core — Master Profile",
                body=self._render_voice_core_body(),
                extra_tags=("voice-core",),
            ),
            _write_skill(
                meta_dir / f"skill-activation-guide{_SKILL_SUFFIX}",
                category="meta",
                key="skill-activation-guide",
                title="Skill Activation Guide",
                body=self._render_activation_guide_body(),
                extra_tags=("activation-guide",),
            ),
        ]
        return written

    # -- Per-category implementations ---------------------------------

    def _generate_frequency_skills(
        self,
        snapshot: VaultSnapshot,
        output_dir: Path,
    ) -> list[Path]:
        """Render one SKILL.md per frequency using *snapshot* exemplars."""
        target_dir = output_dir / _FREQUENCIES_DIR
        by_frequency = _group_fragments_by_frequency(snapshot.fragments)
        written: list[Path] = []
        for freq_key in FREQUENCY_KEYS:
            exemplars = self._pick_exemplars(by_frequency.get(freq_key, []))
            body = self._render_frequency_body(freq_key, exemplars)
            target = target_dir / f"{freq_key}{_SKILL_SUFFIX}"
            written.append(
                _write_skill(
                    target,
                    category="frequency",
                    key=freq_key,
                    title=f"{freq_key}: {FREQUENCY_NAMES[Frequency(freq_key)]}",
                    body=body,
                    extra_tags=(freq_key,),
                ),
            )
        return written

    def _generate_phase_skills(
        self,
        snapshot: VaultSnapshot,
        output_dir: Path,
    ) -> list[Path]:
        """Render one SKILL.md per wavelength phase."""
        target_dir = output_dir / _PHASES_DIR
        by_phase = _group_fragments_by_phase(snapshot.fragments)
        written: list[Path] = []
        for phase_key in PHASE_KEYS:
            exemplars = self._pick_exemplars(by_phase.get(phase_key, []))
            body = self._render_phase_body(phase_key, exemplars)
            target = target_dir / f"{phase_key}{_SKILL_SUFFIX}"
            written.append(
                _write_skill(
                    target,
                    category="phase",
                    key=phase_key,
                    title=f"Phase: {phase_key.replace('_', ' ').title()}",
                    body=body,
                    extra_tags=(phase_key,),
                ),
            )
        return written

    def _generate_mode_skills(
        self,
        snapshot: VaultSnapshot,
        output_dir: Path,
    ) -> list[Path]:
        """Render one SKILL.md per Mode/Orientation pair."""
        target_dir = output_dir / _MODES_DIR
        by_pair = _group_fragments_by_mode_orientation(snapshot.fragments)
        written: list[Path] = []
        for mode_key, orientation_key in MODE_ORIENTATION_KEYS:
            pair = (mode_key, orientation_key)
            exemplars = self._pick_exemplars(by_pair.get(pair, []))
            body = self._render_mode_body(mode_key, orientation_key, exemplars)
            filename = _mode_orientation_key(mode_key, orientation_key)
            title = (
                f"{mode_key.capitalize()}-{orientation_key.replace('_', '/').title()}"
            )
            written.append(
                _write_skill(
                    target_dir / f"{filename}{_SKILL_SUFFIX}",
                    category="mode",
                    key=filename,
                    title=title,
                    body=body,
                    extra_tags=(mode_key, orientation_key),
                ),
            )
        return written

    def _generate_register_skills(
        self,
        snapshot: VaultSnapshot,
        output_dir: Path,
    ) -> list[Path]:
        """Render one SKILL.md per voice register."""
        target_dir = output_dir / _REGISTERS_DIR
        by_register = _group_fragments_by_register(snapshot.fragments)
        written: list[Path] = []
        for register_key in REGISTER_KEYS:
            exemplars = self._pick_exemplars(by_register.get(register_key, []))
            body = self._render_register_body(register_key, exemplars)
            written.append(
                _write_skill(
                    target_dir / f"{register_key}{_SKILL_SUFFIX}",
                    category="register",
                    key=register_key,
                    title=f"Register: {register_key.title()}",
                    body=body,
                    extra_tags=(register_key,),
                ),
            )
        return written

    def _generate_thread_skills(
        self,
        snapshot: VaultSnapshot,
        output_dir: Path,
    ) -> list[Path]:
        """Render one SKILL.md per qualifying thread."""
        target_dir = output_dir / _THREADS_DIR
        qualifying = [
            thread
            for thread in snapshot.threads
            if thread.fragment_count > self.min_thread_fragments
        ]
        written: list[Path] = []
        used_slugs: set[str] = set()
        for thread in qualifying:
            body = self._render_thread_body(thread)
            slug = _unique_slug(_slugify(thread.title), thread.id, used_slugs)
            used_slugs.add(slug)
            written.append(
                _write_skill(
                    target_dir / f"{slug}{_SKILL_SUFFIX}",
                    category="thread",
                    key=thread.id,
                    title=f"Thread: {thread.title}",
                    body=body,
                    extra_tags=("thread",),
                ),
            )
        return written

    def _generate_eddy_skills(
        self,
        snapshot: VaultSnapshot,
        output_dir: Path,
    ) -> list[Path]:
        """Render one SKILL.md per qualifying eddy."""
        target_dir = output_dir / _EDDIES_DIR
        qualifying = [
            eddy
            for eddy in snapshot.eddies
            if eddy.fragment_count > self.min_eddy_fragments
        ]
        written: list[Path] = []
        used_slugs: set[str] = set()
        for eddy in qualifying:
            body = self._render_eddy_body(eddy)
            slug = _unique_slug(_slugify(eddy.title), eddy.id, used_slugs)
            used_slugs.add(slug)
            written.append(
                _write_skill(
                    target_dir / f"{slug}{_SKILL_SUFFIX}",
                    category="eddy",
                    key=eddy.id,
                    title=f"Eddy: {eddy.title}",
                    body=body,
                    extra_tags=("eddy",),
                ),
            )
        return written

    # -- Exemplar selection -------------------------------------------

    def _pick_exemplars(
        self,
        candidates: list[tuple[Fragment, str]],
    ) -> list[SkillExemplar]:
        """Build up to :attr:`max_exemplars` exemplars from *candidates*."""
        exemplars: list[SkillExemplar] = []
        for fragment, body in candidates:
            exemplar = _build_exemplar(fragment, body)
            if exemplar is None:
                continue
            exemplars.append(exemplar)
            if len(exemplars) >= self.max_exemplars:
                break
        return exemplars

    # -- Renderers ----------------------------------------------------

    def _render_frequency_body(
        self,
        freq_key: str,
        exemplars: list[SkillExemplar],
    ) -> str:
        """Render the full markdown body for a Frequency SKILL."""
        freq = Frequency(freq_key)
        name = FREQUENCY_NAMES[freq]
        theme = FREQUENCY_THEMES[freq]
        signals = FREQUENCY_SIGNALS[freq]
        texture = FREQUENCY_VOICE_TEXTURES[freq_key]
        medicine, toxic = FREQUENCY_MEDICINE_VS_TOXIC[freq_key]
        activation = (
            f"# Activation\n\nUse when writing about **{freq_key} / {name}** topics "
            f"— {signals}."
        )
        description = (
            f"{theme}\n\n"
            f"**Voice texture.** {texture}\n\n"
            f"**Medicine expression.** {medicine}\n\n"
            f"**Toxic expression.** {toxic}"
        )
        instructions = _render_bullet_list(
            _frequency_writing_instructions(freq_key),
        )
        anti_patterns = _render_bullet_list(
            _frequency_anti_patterns(freq_key),
        )
        combination = _render_bullet_list(
            self._frequency_combination_hints(freq_key),
        )
        return "\n\n".join(
            [
                activation,
                _render_section("Description", description),
                _render_exemplar_section(exemplars),
                _render_section("Writing Instructions", instructions),
                _render_section("Anti-Patterns", anti_patterns),
                _render_section("Combining With Other Skills", combination),
            ],
        )

    def _render_phase_body(
        self,
        phase_key: str,
        exemplars: list[SkillExemplar],
    ) -> str:
        """Render the full markdown body for a Phase SKILL."""
        rhythm = PHASE_VOICE_RHYTHMS[phase_key]
        human = phase_key.replace("_", " ")
        activation = (
            f"# Activation\n\nUse when writing from the **{human}** phase "
            f"of the Archetypal Wavelength — when energy is "
            f"{_phase_energy_description(phase_key)}."
        )
        description = f"**Voice rhythm.** {rhythm}"
        instructions = _render_bullet_list(_phase_writing_instructions(phase_key))
        anti_patterns = _render_bullet_list(_phase_anti_patterns(phase_key))
        combination = _render_bullet_list(
            [
                f"Layer phase **{human}** over any Frequency skill to shift "
                "cadence and confidence; the Frequency determines the content, "
                "the phase determines the energy.",
                "When pairing with a Register skill, let the phase modulate "
                "sentence length and metaphor family before the register's "
                "stylistic rules kick in.",
            ],
        )
        return "\n\n".join(
            [
                activation,
                _render_section("Description", description),
                _render_exemplar_section(exemplars),
                _render_section("Writing Instructions", instructions),
                _render_section("Anti-Patterns", anti_patterns),
                _render_section("Combining With Other Skills", combination),
            ],
        )

    def _render_mode_body(
        self,
        mode_key: str,
        orientation_key: str,
        exemplars: list[SkillExemplar],
    ) -> str:
        """Render the full markdown body for a Mode/Orientation SKILL."""
        stance = MODE_ORIENTATION_STANCES[(mode_key, orientation_key)]
        orientation_human = orientation_key.replace("_", "/")
        activation = (
            f"# Activation\n\nUse when writing in the "
            f"**{mode_key.title()}-{orientation_human.title()}** "
            f"functional stance — the author is "
            f"{_mode_activation_phrase(mode_key, orientation_key)}."
        )
        description = f"**Functional stance.** {stance}"
        instructions = _render_bullet_list(
            _mode_writing_instructions(mode_key, orientation_key),
        )
        anti_patterns = _render_bullet_list(
            _mode_anti_patterns(mode_key, orientation_key),
        )
        combination = _render_bullet_list(
            [
                "Combine with a Frequency skill to keep the stance tuned to "
                "the right content register.",
                "Combine with a Phase skill to modulate the intensity of the "
                "stance across the wavelength.",
            ],
        )
        return "\n\n".join(
            [
                activation,
                _render_section("Description", description),
                _render_exemplar_section(exemplars),
                _render_section("Writing Instructions", instructions),
                _render_section("Anti-Patterns", anti_patterns),
                _render_section("Combining With Other Skills", combination),
            ],
        )

    def _render_register_body(
        self,
        register_key: str,
        exemplars: list[SkillExemplar],
    ) -> str:
        """Render the full markdown body for a Register SKILL."""
        prompt = REGISTER_VOICE_PROMPTS[register_key]
        anti = REGISTER_ANTI_PATTERNS[register_key]
        activation = (
            f"# Activation\n\nUse when writing in the "
            f"**{register_key.title()}** voice register."
        )
        description = f"**Voice prompt.** {prompt}"
        instructions = _render_bullet_list(
            _register_writing_instructions(register_key),
        )
        combination = _render_bullet_list(
            [
                "Layer on top of a Frequency skill: the register colours how "
                "the frequency's content lands with the reader.",
                "Pair with a Phase skill to tune intensity and cadence.",
            ],
        )
        return "\n\n".join(
            [
                activation,
                _render_section("Description", description),
                _render_exemplar_section(exemplars),
                _render_section(
                    "Writing Instructions",
                    instructions,
                ),
                _render_section("Anti-Patterns", _render_bullet_list(anti)),
                _render_section("Combining With Other Skills", combination),
            ],
        )

    def _render_thread_body(self, thread: Thread) -> str:
        """Render the full markdown body for a Thread SKILL."""
        frequency_affinity = ", ".join(str(f) for f in thread.frequency_affinity) or (
            "(no frequency affinity recorded)"
        )
        description = (
            f"**Status.** {thread.status}\n\n"
            f"**Fragment count.** {thread.fragment_count}\n\n"
            f"**Frequency affinity.** {frequency_affinity}\n\n"
            f"**Description.** {thread.description or '(none recorded)'}"
        )
        activation = (
            f"# Activation\n\nUse when writing about the recurring thread "
            f"**{thread.title}** — its narrative arc, voice register, and "
            "the way it activates specific frequencies over time."
        )
        instructions = _render_bullet_list(
            [
                "Trace the thread's evolution — note where the voice shifted and why.",
                "Anchor claims in the specific fragments that constitute the "
                "thread rather than generalising.",
                "Let the thread's frequency affinities guide the voice texture "
                "without collapsing the thread into a single frequency skill.",
            ],
        )
        anti_patterns = _render_bullet_list(
            [
                "Do not flatten the thread to a single moment.",
                "Do not borrow narrative tropes that aren't present in the "
                "source fragments.",
            ],
        )
        combination = _render_bullet_list(
            [
                "Pair with the relevant Frequency skills from the thread's "
                "``frequency_affinity`` list.",
                "Pair with an Eddy skill when the thread sits inside a "
                "larger topic cluster.",
            ],
        )
        return "\n\n".join(
            [
                activation,
                _render_section("Description", description),
                _render_section("Writing Instructions", instructions),
                _render_section("Anti-Patterns", anti_patterns),
                _render_section("Combining With Other Skills", combination),
            ],
        )

    def _render_eddy_body(self, eddy: Eddy) -> str:
        """Render the full markdown body for an Eddy SKILL."""
        threads_summary = ", ".join(eddy.threads) or "(no threads recorded)"
        description = (
            f"**Fragment count.** {eddy.fragment_count}\n\n"
            f"**Member threads.** {threads_summary}\n\n"
            f"**Description.** {eddy.description or '(none recorded)'}"
        )
        activation = (
            f"# Activation\n\nUse when writing inside the topic cluster "
            f"**{eddy.title}** — the gravitational centre, the recurring "
            "patterns, and the productive contradictions it holds."
        )
        instructions = _render_bullet_list(
            [
                "Name the gravitational centre of the eddy before elaborating.",
                "Surface at least one internal contradiction rather than "
                "smoothing it away.",
                "Use the member threads as lenses rather than as a checklist.",
            ],
        )
        anti_patterns = _render_bullet_list(
            [
                "Do not flatten the eddy to a thesis statement.",
                "Do not ignore contradictions — they are the eddy's energy.",
            ],
        )
        combination = _render_bullet_list(
            [
                "Pair with each Thread skill whose thread sits inside this eddy.",
                "Pair with a Frequency skill when a single frequency "
                "dominates the eddy's emotional climate.",
            ],
        )
        return "\n\n".join(
            [
                activation,
                _render_section("Description", description),
                _render_section("Writing Instructions", instructions),
                _render_section("Anti-Patterns", anti_patterns),
                _render_section("Combining With Other Skills", combination),
            ],
        )

    def _render_voice_core_body(self) -> str:
        """Render the master voice profile meta skill."""
        activation = (
            "# Activation\n\nActivate this meta-skill at the start of any "
            "writing session to anchor the voice across frequencies, phases, "
            "modes, and registers."
        )
        description = (
            "The Creek voice is layered: a **Frequency** skill supplies the "
            "content texture, a **Phase** skill supplies energy and cadence, "
            "a **Mode/Orientation** skill supplies the functional stance, and "
            "a **Register** skill supplies the stylistic colouring. Threads "
            "and Eddies refine the territory."
        )
        instructions = _render_bullet_list(
            [
                "Always establish Frequency first — the content texture "
                "drives every other choice.",
                "Add a Phase skill to decide how much energy the sentence can carry.",
                "Add a Mode/Orientation skill to clarify the author's "
                "functional stance toward the reader.",
                "Add a Register skill last to colour the stylistic surface.",
                "Add Thread and Eddy skills when writing inside specific "
                "territory the human has claimed.",
            ],
        )
        anti_patterns = _render_bullet_list(
            [
                "Do not pick a Register before a Frequency — the register is "
                "a surface, not a source.",
                "Do not skip the Phase skill — voice without phase sounds "
                "temporally flat.",
                "Do not stack more than two Register skills at once — the "
                "colouring cancels out.",
            ],
        )
        combination = _render_bullet_list(
            [
                "See ``skill-activation-guide`` for concrete layering recipes.",
                "When in doubt, start with the Frequency skill and add one "
                "other skill at a time.",
            ],
        )
        return "\n\n".join(
            [
                activation,
                _render_section("Description", description),
                _render_section("Writing Instructions", instructions),
                _render_section("Anti-Patterns", anti_patterns),
                _render_section("Combining With Other Skills", combination),
            ],
        )

    def _render_activation_guide_body(self) -> str:
        """Render the skill activation guide meta skill."""
        activation = (
            "# Activation\n\nRead this meta-skill whenever you need to decide "
            "which skills to activate for a given writing task."
        )
        description = (
            "Skills in the Creek Voice Skill Tree are designed to be "
            "**stacked**, not substituted. A typical activation set has four "
            "or five layers: one Frequency, one Phase, one Mode/Orientation, "
            "one Register, plus optional Thread or Eddy skills."
        )
        recipe_lines = [
            "**Blog post from F5/Orange during Peaking in an Express-Do "
            "stance using the Analytical register** → `F5`, `peaking`, "
            "`express-do`, `analytical`.",
            "**Grief letter in F6 during Bottoming Out in an "
            "Inhabit-Feel stance using the Confessional register** → "
            "`F6`, `bottoming_out`, `inhabit-feel`, `confessional`.",
            "**Instruction manual in F1 during Rising in an Express-Do "
            "stance using the Instructional register** → `F1`, `rising`, "
            "`express-do`, `instructional`.",
        ]
        instructions = _render_bullet_list(
            [
                "Pick exactly one skill from each of Frequency, Phase, "
                "Mode/Orientation, Register.",
                "Add Thread or Eddy skills only when writing about a "
                "specific territory.",
                "If two skills conflict, the more specific skill "
                "(Thread > Eddy > Frequency > Register) wins.",
            ],
        )
        anti_patterns = _render_bullet_list(
            [
                "Do not activate every skill — coverage is not the goal; coherence is.",
                "Do not pick mutually exclusive registers.",
            ],
        )
        combination = _render_bullet_list(recipe_lines)
        return "\n\n".join(
            [
                activation,
                _render_section("Description", description),
                _render_section("Writing Instructions", instructions),
                _render_section("Anti-Patterns", anti_patterns),
                _render_section("Combining With Other Skills", combination),
            ],
        )

    # -- Combination hint helpers -------------------------------------

    def _frequency_combination_hints(self, freq_key: str) -> list[str]:
        """Return the bullet list of combination hints for *freq_key*."""
        hints: list[str] = [
            f"Layer a Phase skill on top of {freq_key} to decide whether "
            "the voice rises, peaks, or withdraws.",
            f"Add a Register skill to colour how {freq_key} content lands "
            "with the reader.",
        ]
        phase_hint = _FREQUENCY_PHASE_HINTS.get(freq_key)
        if phase_hint:
            hints.append(phase_hint)
        register_hint = _FREQUENCY_REGISTER_HINTS.get(freq_key)
        if register_hint:
            hints.append(register_hint)
        return hints


# ---- Grouping helpers ----


def _group_fragments_by_frequency(
    fragments: tuple[tuple[Fragment, str], ...],
) -> dict[str, list[tuple[Fragment, str]]]:
    """Group *fragments* by their primary frequency key."""
    grouped: dict[str, list[tuple[Fragment, str]]] = {}
    for fragment, body in fragments:
        primary = str(fragment.frequency.primary)
        if primary == Frequency.UNCLASSIFIED.value:
            continue
        grouped.setdefault(primary, []).append((fragment, body))
    return grouped


def _group_fragments_by_phase(
    fragments: tuple[tuple[Fragment, str], ...],
) -> dict[str, list[tuple[Fragment, str]]]:
    """Group *fragments* by their wavelength phase."""
    grouped: dict[str, list[tuple[Fragment, str]]] = {}
    for fragment, body in fragments:
        phase = str(fragment.wavelength.phase)
        if phase == Phase.UNCLASSIFIED.value:
            continue
        grouped.setdefault(phase, []).append((fragment, body))
    return grouped


def _group_fragments_by_mode_orientation(
    fragments: tuple[tuple[Fragment, str], ...],
) -> dict[tuple[str, str], list[tuple[Fragment, str]]]:
    """Group *fragments* by their ``(mode, orientation)`` pair."""
    grouped: dict[tuple[str, str], list[tuple[Fragment, str]]] = {}
    for fragment, body in fragments:
        mode = str(fragment.wavelength.mode)
        orientation = str(fragment.wavelength.orientation)
        if mode == Mode.UNCLASSIFIED.value:
            continue
        if orientation == Orientation.UNCLASSIFIED.value:
            continue
        if mode == Mode.ABSORB.value:
            key = (Mode.ABSORB.value, "do_feel")
        else:
            key = (mode, orientation)
        grouped.setdefault(key, []).append((fragment, body))
    return grouped


def _group_fragments_by_register(
    fragments: tuple[tuple[Fragment, str], ...],
) -> dict[str, list[tuple[Fragment, str]]]:
    """Group *fragments* by their voice register."""
    grouped: dict[str, list[tuple[Fragment, str]]] = {}
    for fragment, body in fragments:
        register = fragment.voice.voice_register
        if register is None:
            continue
        key = str(register)
        grouped.setdefault(key, []).append((fragment, body))
    return grouped


# ---- Per-category instruction helpers ----


def _frequency_writing_instructions(freq_key: str) -> list[str]:
    """Return writing bullets for the given frequency."""
    texture = FREQUENCY_VOICE_TEXTURES[freq_key]
    return [
        f"Keep the voice texture: {texture}",
        "Name at least one concrete metaphor drawn from the frequency's "
        "signature imagery.",
        "Let sentence length track the frequency's typical cadence rather "
        "than uniform prose.",
    ]


def _frequency_anti_patterns(freq_key: str) -> list[str]:
    """Return per-frequency anti-patterns."""
    _, toxic = FREQUENCY_MEDICINE_VS_TOXIC[freq_key]
    return [
        "Do not drift into the toxic expression of this frequency "
        f"(characterised by: {toxic.lower()})",
        "Do not borrow metaphors from another frequency's signature imagery "
        "without deliberate contrast.",
    ]


def _phase_writing_instructions(phase_key: str) -> list[str]:
    """Return writing bullets for the given phase."""
    rhythm = PHASE_VOICE_RHYTHMS[phase_key]
    return [
        f"Carry the phase's rhythm: {rhythm}",
        "Keep the sentence length consistent with the phase before "
        "layering on register-driven flourishes.",
        "Let the phase decide what topics belong on the page.",
    ]


def _phase_anti_patterns(phase_key: str) -> list[str]:
    """Return anti-patterns for the given phase."""
    return [
        f"Do not force {phase_key.replace('_', ' ')} energy to sound like a "
        "neighbouring phase.",
        "Do not paper over a diminishing or bottoming-out phase with "
        "performative upswing.",
    ]


def _phase_energy_description(phase_key: str) -> str:
    """Return a short phrase describing phase energy for activation text."""
    descriptions = {
        "rising": "gathering",
        "peaking": "at full height",
        "withdrawal": "receding by choice",
        "diminishing": "leaking",
        "bottoming_out": "at its lowest",
        "restoration": "returning quietly",
    }
    return descriptions[phase_key]


def _mode_writing_instructions(mode: str, orientation: str) -> list[str]:
    """Return writing bullets for the Mode/Orientation pair."""
    stance = MODE_ORIENTATION_STANCES[(mode, orientation)]
    return [
        f"Embody the stance: {stance}",
        "Keep pronouns consistent with the stance (first-person singular "
        "for Inhabit, inclusive plural for Collaborate, second-person for "
        "Express, retrospective first-person for Integrate).",
        "Let orientation decide whether the paragraph resolves in action "
        "(``do``), feeling (``feel``), or receptive stillness (``do_feel``).",
    ]


def _mode_anti_patterns(mode: str, orientation: str) -> list[str]:
    """Return anti-patterns for the Mode/Orientation pair."""
    return [
        f"Do not drift out of the **{mode}-{orientation}** stance mid-paragraph.",
        "Do not conflate Inhabit with Express — inhabiting is private, "
        "expressing is public.",
    ]


def _mode_activation_phrase(mode: str, orientation: str) -> str:
    """Return a short activation phrase for the Mode/Orientation pair."""
    base = {
        "inhabit": "inside the frequency",
        "express": "enacting the frequency for others",
        "collaborate": "working alongside others in the frequency",
        "integrate": "metabolising the frequency into coherence",
        "absorb": "receiving the frequency rather than producing it",
    }[mode]
    if orientation == "do":
        return f"{base}, oriented toward action"
    if orientation == "feel":
        return f"{base}, oriented toward feeling"
    return f"{base}, oriented toward receptive presence"


def _register_writing_instructions(register_key: str) -> list[str]:
    """Return writing bullets for the given register."""
    prompt = REGISTER_VOICE_PROMPTS[register_key]
    return [
        f"Honour the register prompt: {prompt}",
        "Apply the register last so it colours the stylistic surface "
        "rather than dictating the underlying content.",
        "If the register's anti-patterns conflict with an active "
        "Frequency's voice texture, prefer the Frequency's texture and "
        "soften the register.",
    ]
