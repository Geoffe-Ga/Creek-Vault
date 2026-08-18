"""Draft generation — skill-activated essay drafts from idea seeds.

Section 11.5 of the Creek Ontology describes the draft workflow: when
the human selects an :class:`IdeaSeed`, the system assembles a skill
stack (frequency, phase, mode, register SKILL.md files), pulls the
source fragments and thread/eddy context, and asks an LLM to write a
draft that sounds like the human.

The draft is saved to ``07-Voice/Drafts/`` with full provenance in
frontmatter so a reader can trace every sentence back to its roots:

* ``source_fragments`` — IDs of the fragments that seeded the idea.
* ``threads`` / ``eddies`` — related narrative and topic clusters.
* ``activated_skills`` — relative paths of SKILL.md files used.
* ``prompt`` — the full LLM prompt (for reproducibility).

The LLM client is injected as a :data:`DraftLLM` callable so the module
stays deterministic and test-friendly; no network calls live in this
module.

A caller that cannot know the routing tier when it builds the generator
injects a :data:`DraftLLMFactory` instead (#1031). ``creek draft`` picks
its source fragments *here*, several layers below the CLI, so a client
built up there could only ever be tier-less — and a tier-less client is a
:meth:`creek.classify.llm.router.ModelRouter.resolve` with the
``Intimate``-never-cloud gate (#647) switched off. The generator surveys
the tiers of the fragments each prompt renders and calls the factory with
their maximum; see :meth:`DraftGenerator._bind_routing_tier`.
"""

from __future__ import annotations

import hashlib
import logging
import operator
import re
import sys
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import frontmatter
import yaml
from pydantic import ValidationError

from creek.care.guardrail import CARE_POLICY
from creek.classify.privacy_filter import (
    PrivacyTierOverride,
    filter_fragments_by_tier,
    max_source_tier,
    source_tiers,
)
from creek.generate.ai_style.guard import (
    VOICE_GUARD_STATUS_KEY,
    run_voice_fidelity_guard,
)
from creek.generate.cohesion import run_cohesion_pass
from creek.generate.compile_routing import (
    CompiledPageIndex,
    empty_index,
    load_compiled_pages,
    log_compile_gap,
)
from creek.generate.dimensional_retrieval import (
    AllDimensionsEmptyError,
    DimensionKey,
    DimensionSlice,
    assemble_per_dimension_corpus,
    empty_dimensions,
    format_dimension_label,
    raise_if_all_empty,
    union_fragment_ids,
)
from creek.generate.grounding import (
    EmbeddingFn,
    GroundingReport,
    GroundingThresholds,
    ParagraphAnnotation,
    build_grounding_frontmatter,
    scan_biographical_sentences,
    score_draft,
)
from creek.generate.ontology_glossary import GLOSS_STEER
from creek.generate.outline import (
    OutlineSection,
    build_stitch_prompt,
    parse_outline,
)
from creek.generate.twist import (
    OntologyProfile,
    format_twist_directive,
    require_plural_sources,
    source_profile_from_fragments,
    target_profile_from_spec,
    twist_dimensions,
)
from creek.link.embeddings import EmbeddingModelUnavailableError
from creek.models import (
    CompiledPage,
    Eddy,
    Fragment,
    Frequency,
    Mode,
    Phase,
    PrivacyTier,
    Thread,
)

if TYPE_CHECKING:
    from pathlib import Path

    from creek.classify.prompt import PromptOntology
    from creek.config import AIStyleConfig
    from creek.generate.ai_style.guard import GroundingCheck
    from creek.generate.ai_style.model import VoiceFingerprint
    from creek.generate.mining import IdeaSeed


logger = logging.getLogger(__name__)


DRAFTS_SUBDIR: str = "07-Voice/Drafts"
"""Relative path under the vault where drafts are stored."""

_FRAGMENTS_SUBDIR: str = "01-Fragments"
_THREADS_SUBDIR: str = "02-Threads"
_EDDIES_SUBDIR: str = "03-Eddies"

_SKILL_SUFFIX: str = ".SKILL.md"
_FREQUENCIES_DIR: str = "frequencies"
_PHASES_DIR: str = "phases"
_MODES_DIR: str = "modes"
_REGISTERS_DIR: str = "registers"

_SLUG_MAX_LENGTH: int = 80

_BESPOKE_ONTOLOGY_TERMS: tuple[str, ...] = (
    "fragment",
    "fragments",
    "resonance",
    "resonances",
    "praxis",
    "praxes",
    "wavelength",
    "wavelengths",
    "aptitude",
)
"""The owner's *distinctive* bespoke Creek ontology vocabulary.

Guarded **case-insensitively** by the cohesion entity-preservation check
alongside proper nouns and numbers: a cohesion pass that introduces one of
these terms where the pre-cohesion body had none is fabricating ontology
structure, not adding a transition. Lowercased on purpose — these terms are
unambiguous ontology references even in lowercase prose, so a mid-sentence
``Wavelength`` and a lowercased ``wavelength`` are both caught.

Common English words that double as ontology terms (Thread, Eddy and their
plurals) are *not* listed here; they live in
:data:`_COMMON_CAPITALIZED_ONTOLOGY_TERMS` and are guarded case-sensitively so
ordinary prose like "the eddies of thought" is not mistaken for fabrication."""

_COMMON_CAPITALIZED_ONTOLOGY_TERMS: tuple[str, ...] = (
    "Thread",
    "Threads",
    "Eddy",
    "Eddies",
)
"""Ontology terms that are also common English words.

Guarded **case-sensitively**, in their Capitalized ontological form only, by
the cohesion entity-preservation check. ``Eddy``/``Thread`` introduced where
the pre-cohesion body had none is fabricated ontology structure (and is also
caught as a proper noun); but a lowercase transition like "the eddies of
thought" is ordinary prose and must NOT be rejected — so the lowercase form is
deliberately left unguarded here."""

_MIN_TWIST_SOURCES: int = 2
"""Minimum source fragments before a section composes through the twist path.

Mirrors the plurality floor in :mod:`creek.generate.twist`. A section with
fewer matched fragments drops the twist directive and composes plainly so
it still appears in the outline draft rather than aborting the whole essay
the way the single-topic path does.
"""


@dataclass(frozen=True)
class DraftLLMResponse:
    """A draft LLM response paired with the model's stop reason.

    A draft LLM may return either a plain ``str`` (legacy callers and
    tests) or this richer object. The text is the draft body; the stop
    reason lets the generator tell a clean completion apart from a
    ``max_tokens`` truncation so a cut-off essay is flagged rather than
    saved silently.

    Attributes:
        text: The draft body returned by the model.
        stop_reason: Why the model stopped. ``"end_turn"`` on a clean
            completion; ``"max_tokens"`` when the ceiling was hit.
    """

    text: str
    stop_reason: str = "end_turn"


DraftLLM = Callable[[str], "str | DraftLLMResponse"]
"""Signature for draft LLM clients.

Takes a prompt and returns either the draft body as a plain ``str`` or a
:class:`DraftLLMResponse` carrying the body plus the model's stop reason.
A bare string is treated as a clean (``"end_turn"``) completion so legacy
callers keep working; the CLI adapter returns the richer object so a
``max_tokens`` truncation can be surfaced.
"""


DraftLLMFactory = Callable[[PrivacyTier], DraftLLM]
"""Signature for tier-keyed draft LLM factories (#1031).

Takes the privacy tier of the fragments a prompt will carry and returns
the :data:`DraftLLM` client that tier may be sent to. It is the draft
twin of ``creek.compile.engine.CompileLLMFactory``, and it exists for the
same reason: the ``Intimate``-never-cloud chokepoint (#647) is
:meth:`creek.classify.llm.router.ModelRouter.resolve`'s ``tier``
argument, and only the layer that has seen the source fragments can
supply it. ``creek draft`` picks its sources *inside* this module — long
after the CLI built a client — so a pre-built client can only ever be a
tier-less one, which is exactly how intimate bodies came to be routable
to a cloud provider under ``--include-tier intimate|all``.

The factory may be called more than once per draft (the outline path
composes one prompt per section plus a stitch, and the tiers can differ),
so a production factory should cache per tier rather than re-handshake.
"""


OntologyDetector = Callable[[str], "PromptOntology"]
"""Signature for prompt-ontology detectors.

Takes a section's free-form seed text, returns a
:class:`~creek.classify.prompt.PromptOntology`. Injected into
:meth:`DraftGenerator.generate_outline_draft` so the module stays
deterministic and provider-free — the CLI passes a closure over
:func:`creek.classify.prompt.detect_ontology` bound to the resolved
:class:`~creek.config.LLMConfig`.
"""


_MAX_TOKENS_STOP_REASON: str = "max_tokens"
"""Stop reason the LLM reports when output was cut off at the token ceiling."""

_TRUNCATION_WARNING: str = (
    "[creek draft] WARNING: draft truncated at max_tokens; "
    "raise --max-tokens or simplify the prompt"
)
"""Stderr warning emitted when a draft is cut off at the token ceiling."""

_TRUNCATION_FOOTER: str = "\n\n---\n*(truncated — see frontmatter)*"
"""Footer appended to a truncated draft body so the file is self-describing."""


def _fixed_llm_factory(llm: DraftLLM) -> DraftLLMFactory:
    """Adapt a pre-built *llm* to the :data:`DraftLLMFactory` shape.

    Lets :class:`DraftGenerator` hold exactly one client seam while both
    constructions stay supported: ``llm=`` for a caller that already keyed
    its client by tier (``creek_mcp.tools.draft`` derives the tier before
    the generator exists) or that injects a test double, and
    ``llm_factory=`` for a caller that cannot know the tier yet (the
    ``creek draft`` CLI).

    Args:
        llm: The already-built draft client.

    Returns:
        A factory returning *llm* for every tier. The tier is discarded
        deliberately — a caller passing a built client has taken the
        routing decision itself, and re-deciding it here would silently
        override it.
    """

    def _factory(_tier: PrivacyTier) -> DraftLLM:
        """Return the pre-built client, whatever the tier."""
        return llm

    return _factory


def _normalise_llm_response(raw: str | DraftLLMResponse) -> DraftLLMResponse:
    """Coerce a draft LLM return value into a :class:`DraftLLMResponse`.

    A bare ``str`` is wrapped as a clean ``"end_turn"`` completion so the
    legacy callable signature keeps working unchanged.
    """
    if isinstance(raw, DraftLLMResponse):
        return raw
    return DraftLLMResponse(text=raw)


def _warn_if_truncated(stop_reason: str) -> bool:
    """Return whether *stop_reason* marks a truncation, warning on stderr.

    Prints :data:`_TRUNCATION_WARNING` to stderr when the model hit the
    ``max_tokens`` ceiling so an operator running ``creek draft`` sees the
    essay is incomplete before the saved-path message appears.
    """
    if stop_reason != _MAX_TOKENS_STOP_REASON:
        return False
    print(_TRUNCATION_WARNING, file=sys.stderr)
    return True


def _draft_file_body(body: str, *, truncated: bool) -> str:
    """Return the persisted body, appending the truncation footer when cut off.

    The footer makes a truncated file self-describing when opened later
    without the frontmatter in view.
    """
    text = body.strip() + "\n"
    if truncated:
        text += _TRUNCATION_FOOTER + "\n"
    return text


def _stitch_prompt_digest(stitch_prompt: str) -> str:
    """Return a short content hash of *stitch_prompt* for frontmatter.

    The full stitch prompt embeds every section body and can run to
    10-15 KB; persisting it verbatim bloats the YAML frontmatter enough
    to slow Obsidian's parse. A content-addressable digest keeps the
    provenance link without the bulk — the outline input is already
    recorded verbatim, so the prompt stays reproducible.
    """
    return hashlib.sha256(stitch_prompt.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class SeedSpec:
    """Manual seed specification for ``creek draft`` (FEAT-032).

    Each field is optional and combinable per the FEAT-032 rules:

    * ``fragment_id`` is mutually exclusive with every other field — when
      it is set, the draft follows on from one specific fragment.
    * ``topic`` may be combined with any dimensional filter to narrow
      the candidate source material.
    * The three dimensional filters (frequencies / phases / modes) AND
      together: a candidate fragment must satisfy every supplied
      dimension. An empty tuple means "do not filter on this dimension".

    Attributes:
        fragment_id: Specific fragment ID to draft a follow-up from.
        topic: Free-form topic phrase guiding the essay.
        frequencies: APTITUDE frequencies to restrict candidates to.
        phases: Wavelength phases to restrict candidates to.
        modes: Wavelength modes (stances) to restrict candidates to.
        ontology: Optional :class:`~creek.classify.prompt.PromptOntology`
            from prompt-level detection. When set, its weighted
            dimensions augment the explicit-flag dimensions during
            per-dimension corpus retrieval. An empty ontology is
            equivalent to ``None``.
        ontology_confidence_threshold: Minimum weight an ontology
            entry must carry to participate in the per-dimension blend.
            Defaults to ``0.0`` so every parsed entry is honoured;
            callers with a configured FEAT-017 floor pass it explicitly.
    """

    fragment_id: str | None = None
    topic: str | None = None
    frequencies: tuple[Frequency, ...] = ()
    phases: tuple[Phase, ...] = ()
    modes: tuple[Mode, ...] = ()
    ontology: PromptOntology | None = None
    ontology_confidence_threshold: float = 0.0

    def __post_init__(self) -> None:
        """Normalise empty topics, then enforce the ``fragment_id`` rule."""
        if self.topic is not None and not self.topic.strip():
            # Whitespace-only topics produce confusing zero-match errors
            # downstream; collapse them to ``None`` so the spec is honestly
            # empty. ``object.__setattr__`` is required on a frozen dataclass.
            object.__setattr__(self, "topic", None)
        if self.fragment_id is None:
            return
        conflicts = [
            name
            for name, value in (
                ("topic", self.topic),
                ("frequencies", self.frequencies),
                ("phases", self.phases),
                ("modes", self.modes),
                ("ontology", self.ontology),
            )
            if value
        ]
        if conflicts:
            msg = (
                f"SeedSpec.fragment_id is mutually exclusive with "
                f"{', '.join(conflicts)}"
            )
            raise ValueError(msg)

    @property
    def is_empty(self) -> bool:
        """Return ``True`` when no manual seed dimension is set."""
        return not (
            self.fragment_id
            or self.topic
            or self.frequencies
            or self.phases
            or self.modes
            or self.ontology
        )

    @property
    def has_dimensional_filter(self) -> bool:
        """Return ``True`` when at least one classification filter is set.

        Includes prompt-detected :class:`PromptOntology` dimensions so a
        purely-ontology-driven spec still flips into the per-dimension
        retrieval path.
        """
        if self.frequencies or self.phases or self.modes:
            return True
        if self.ontology is None:
            return False
        return bool(
            self.ontology.frequencies or self.ontology.phases or self.ontology.modes,
        )


class SeedResolutionError(ValueError):
    """Raised when a :class:`SeedSpec` cannot be resolved to any candidates.

    The CLI surfaces the message to the operator as an honest "no
    matching source material" error rather than silently falling back
    to unfiltered drafting (FEAT-032 acceptance criterion).
    """


@dataclass(frozen=True)
class _TwistPayload:
    """Bundle of twist-composition outputs threaded into :class:`Draft`.

    The default-constructed instance represents "no twist": the
    directive is empty so :meth:`DraftGenerator._compose_prompt` skips
    the block, both profiles stay ``None`` so the frontmatter writer
    omits them, and the dimensions tuple is empty.
    """

    directive: str = ""
    source_profile: OntologyProfile | None = None
    target_profile: OntologyProfile | None = None
    dimensions: tuple[str, ...] = ()


@dataclass(frozen=True)
class SectionComposition:
    """One composed outline section with its per-section ontology profiles.

    Attributes:
        heading: The section header text (``#`` markers stripped).
        level: The header depth (1-6).
        body: The LLM-composed section body before the stitch pass.
        source_profile: Ontology profile aggregated from the section's
            retrieved source fragments. :data:`None` when the section
            matched no source material.
        target_profile: Ontology profile asked for by the section's
            detected :class:`~creek.classify.prompt.PromptOntology`.
            Always populated — the detector runs on every section.
        twist_dimensions: Ordered dimension names where the section's
            source profile diverges from its target profile. Empty when
            the profiles match or no source material was found.
        source_fragments: Fragment IDs that fed this section's prompt.
        truncated: Whether this section's body was cut off at the LLM's
            ``max_tokens`` ceiling.
    """

    heading: str
    level: int
    body: str
    target_profile: OntologyProfile
    source_profile: OntologyProfile | None = None
    twist_dimensions: tuple[str, ...] = ()
    source_fragments: tuple[str, ...] = ()
    truncated: bool = False


@dataclass(frozen=True)
class OutlineDraft:
    """A multi-section draft composed from a markdown outline.

    The per-section bodies are composed independently (detect → retrieve
    → compose) and then stitched into one continuous essay with a
    transition-only smoothing pass.

    Attributes:
        title: The draft title (the first section's heading).
        body: The stitched essay body.
        sections: The composed sections in document order, each carrying
            its own ontology profiles for frontmatter provenance.
        generated_date: When the outline draft was generated.
        outline_text: The original outline input, recorded verbatim for
            reproducibility.
        stitch_prompt: The full stitch-pass prompt sent to the LLM. Kept
            on the in-memory object for inspection but deliberately *not*
            persisted verbatim — the stitched bodies make it 10-15 KB,
            large enough to slow Obsidian's frontmatter parse. The saved
            file records only a short content hash (see
            :meth:`DraftGenerator.save_outline_draft`); the outline input
            already captured above makes the prompt reproducible.
        truncated: Whether the stitch pass was cut off at the LLM's
            ``max_tokens`` ceiling, or any section body was truncated.
    """

    title: str
    body: str
    sections: tuple[SectionComposition, ...]
    generated_date: datetime
    outline_text: str
    stitch_prompt: str
    truncated: bool = False

    def __post_init__(self) -> None:
        """Validate outline-draft invariants."""
        if not self.title.strip():
            msg = "OutlineDraft.title must be non-empty"
            raise ValueError(msg)
        if not self.body.strip():
            msg = "OutlineDraft.body must be non-empty"
            raise ValueError(msg)
        if not self.sections:
            msg = "OutlineDraft.sections must be non-empty"
            raise ValueError(msg)


@dataclass(frozen=True)
class Draft:
    """An LLM-generated essay draft with full provenance.

    Attributes:
        title: Working title for the essay.
        body: The generated draft text.
        idea_strategy: :class:`MiningStrategy` value that surfaced the idea.
        source_fragments: Fragment IDs that seeded the idea.
        threads: Related thread IDs.
        eddies: Related eddy IDs.
        skill_stack: Relative paths of activated SKILL.md files.
        prompt: The full LLM prompt (for reproducibility).
        generated_date: When the draft was generated.
        seed_spec: Manual-seed specification (FEAT-032) when the draft
            was produced via ``creek draft --seed-*`` flags; ``None``
            for drafts surfaced via the idea miner.
        per_dimension_sources: Per-dimension corpus attribution. Maps a
            ``"kind:value"`` label (e.g., ``"phase:peaking"``) to the
            tuple of fragment IDs that entered the LLM prompt as that
            dimension's labelled section. Empty when the draft used the
            legacy single-dimension path; populated whenever
            :func:`assemble_per_dimension_corpus` ran.
        source_profile: Ontology profile aggregated from the retrieved
            source fragments. ``None`` when the draft did not pass
            through the twist composition path.
        target_profile: Ontology profile asked for by the operator
            (explicit flags + detected :class:`PromptOntology`). ``None``
            when the draft did not pass through the twist composition
            path.
        twist_dimensions: Ordered tuple of dimension names where the
            source profile diverges from the target profile. Empty when
            the two profiles match or when twist composition was not
            requested.
        derivative_score: Bidirectional grounding guard's derivative
            metric. The maximum paragraph cosine similarity recorded
            between the draft body and every paragraph of the source
            fragments that fed the prompt. ``None`` when the guard did
            not run (no embedding callable wired) so the field reads as
            "not yet evaluated" rather than "evaluated clean".
        grounding_score: Bidirectional grounding guard's grounding
            metric. The fraction of draft paragraphs whose
            ``max_similarity`` cleared
            :attr:`creek.config.DraftConfig.grounding_lower` against
            *some* source paragraph. ``None`` when the guard did not
            run.
        paragraph_grounding: Per-paragraph annotation list produced by
            :class:`creek.generate.grounding.GroundingReport`. Each
            entry carries ``{index, max_similarity, is_derivative,
            is_grounded}``. Empty when the guard did not run.
        truncated: Whether the draft body was cut off at the LLM's
            ``max_tokens`` ceiling.
    """

    title: str
    body: str
    idea_strategy: str
    source_fragments: tuple[str, ...]
    threads: tuple[str, ...]
    eddies: tuple[str, ...]
    skill_stack: tuple[str, ...]
    prompt: str
    generated_date: datetime
    seed_spec: SeedSpec | None = None
    per_dimension_sources: tuple[tuple[str, tuple[str, ...]], ...] = ()
    source_profile: OntologyProfile | None = None
    target_profile: OntologyProfile | None = None
    twist_dimensions: tuple[str, ...] = ()
    derivative_score: float | None = None
    grounding_score: float | None = None
    paragraph_grounding: tuple[ParagraphAnnotation, ...] = field(default_factory=tuple)
    truncated: bool = False

    def __post_init__(self) -> None:
        """Validate draft invariants."""
        if not self.title.strip():
            msg = "Draft.title must be non-empty"
            raise ValueError(msg)
        if not self.body.strip():
            msg = "Draft.body must be non-empty"
            raise ValueError(msg)


def _safe_post(md_file: Path) -> frontmatter.Post | None:
    """Load a frontmatter post, returning ``None`` on parse errors."""
    try:
        return frontmatter.load(str(md_file))
    except (OSError, ValueError, yaml.YAMLError):
        logger.debug("Skipping unreadable markdown: %s", md_file)
        return None


def _load_fragments_by_id(
    root: Path,
    *,
    privacy_override: PrivacyTierOverride | None = None,
) -> dict[str, tuple[Fragment, str]]:
    """Return ``{fragment.id: (fragment, body)}`` for every fragment under *root*.

    The privacy override is forwarded to
    :func:`~creek.classify.privacy_filter.filter_fragments_by_tier` so
    intimate content never leaks into the draft prompt under default
    policy and personal bodies are replaced by title-only summaries.
    """
    if not root.exists():
        return {}
    pairs: list[tuple[Fragment, str]] = []
    for md_file in sorted(root.rglob("*.md")):
        post = _safe_post(md_file)
        if post is None:
            continue
        metadata = dict(post.metadata)
        if metadata.get("type") != "fragment":
            continue
        try:
            fragment = Fragment.model_validate(metadata)
        except ValidationError:
            logger.debug("Skipping invalid fragment frontmatter: %s", md_file)
            continue
        pairs.append((fragment, post.content))
    filtered = filter_fragments_by_tier(pairs, override=privacy_override)
    return {f.id: (f, body) for f, body in filtered}


def _load_threads_by_id(root: Path) -> dict[str, Thread]:
    """Return ``{thread.id: thread}`` for every thread under *root*."""
    if not root.exists():
        return {}
    collected: dict[str, Thread] = {}
    for md_file in sorted(root.rglob("*.md")):
        post = _safe_post(md_file)
        if post is None:
            continue
        metadata = dict(post.metadata)
        if metadata.get("type") != "thread":
            continue
        try:
            thread = Thread.model_validate(metadata)
        except ValidationError:
            logger.debug("Skipping invalid thread frontmatter: %s", md_file)
            continue
        collected[thread.id] = thread
    return collected


def _load_eddies_by_id(root: Path) -> dict[str, Eddy]:
    """Return ``{eddy.id: eddy}`` for every eddy under *root*."""
    if not root.exists():
        return {}
    collected: dict[str, Eddy] = {}
    for md_file in sorted(root.rglob("*.md")):
        post = _safe_post(md_file)
        if post is None:
            continue
        metadata = dict(post.metadata)
        if metadata.get("type") != "eddy":
            continue
        try:
            eddy = Eddy.model_validate(metadata)
        except ValidationError:
            logger.debug("Skipping invalid eddy frontmatter: %s", md_file)
            continue
        collected[eddy.id] = eddy
    return collected


def _fragment_matches_dimensions(
    fragment: Fragment,
    *,
    frequencies: tuple[Frequency, ...] = (),
    phases: tuple[Phase, ...] = (),
    modes: tuple[Mode, ...] = (),
) -> bool:
    """Return ``True`` when *fragment* matches every supplied dimension.

    An empty tuple for a given dimension means "do not filter on it".
    Fragments persisted with ``use_enum_values=True`` store the enum's
    ``.value``, so comparisons happen against the StrEnum value to
    cover both the in-memory enum and the on-disk string form.
    """
    if frequencies:
        primary = str(fragment.frequency.primary)
        if primary not in {freq.value for freq in frequencies}:
            return False
    if phases:
        phase = str(fragment.wavelength.phase)
        if phase not in {ph.value for ph in phases}:
            return False
    if modes:
        mode = str(fragment.wavelength.mode)
        if mode not in {md.value for md in modes}:
            return False
    return True


def _topic_matches_fragment(fragment: Fragment, body: str, topic: str) -> bool:
    """Return ``True`` when *topic* appears in fragment title or body.

    Case-insensitive substring match — the v1 surface for
    ``--seed-topic``. Embedding-based retrieval is a documented
    follow-up; substring matching is honest about what the heuristic
    can promise on a young vault without an embeddings index.
    """
    needle = topic.strip().lower()
    if not needle:
        return False
    if needle in fragment.title.lower():
        return True
    return needle in body.lower()


def _seed_dimension_descriptor(spec: SeedSpec) -> str:
    """Render the dimensional intersection of *spec* as ``F1 x rising x inhabit``."""
    parts: list[str] = []
    if spec.frequencies:
        parts.append(" / ".join(freq.value for freq in spec.frequencies))
    if spec.phases:
        parts.append(" / ".join(ph.value for ph in spec.phases))
    if spec.modes:
        parts.append(" / ".join(md.value for md in spec.modes))
    return " x ".join(parts)


def _dominant(values: list[str]) -> str | None:
    """Return the most common non-empty string in *values*, or ``None``."""
    counts: dict[str, int] = {}
    for value in values:
        if not value:
            continue
        counts[value] = counts.get(value, 0) + 1
    if not counts:
        return None
    return max(counts.items(), key=operator.itemgetter(1, 0))[0]


def _dominant_classified(values: Iterable[object]) -> str | None:
    """Return the dominant classified (non-unclassified) value as a string."""
    stringified = [str(v) for v in values]
    filtered = [s for s in stringified if s and s != "unclassified"]
    return _dominant(filtered)


def _slugify(text: str) -> str:
    """Return a filesystem-safe slug for *text*, truncated to ``_SLUG_MAX_LENGTH``."""
    cleaned = re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-").lower()
    if not cleaned:
        cleaned = "draft"
    if len(cleaned) > _SLUG_MAX_LENGTH:
        cleaned = cleaned[:_SLUG_MAX_LENGTH].rstrip("-")
    return cleaned


def _next_available_draft_path(
    drafts_dir: Path,
    date_prefix: str,
    slug: str,
) -> Path:
    """Return a non-colliding draft path under *drafts_dir*.

    Same-day drafts of the same title are otherwise silently overwritten;
    this helper appends ``-2``, ``-3``, … until an unused path is found.
    """
    base = drafts_dir / f"{date_prefix}-{slug}.md"
    if not base.exists():
        return base
    counter = 2
    while True:
        candidate = drafts_dir / f"{date_prefix}-{slug}-{counter}.md"
        if not candidate.exists():
            return candidate
        counter += 1


class DraftGenerator:
    """Generate an essay draft from an :class:`IdeaSeed` via a skill stack.

    Attributes:
        skills_root: Directory containing the SKILL.md tree.
        voice_core: Optional voice-core description prepended to prompts.
        style_preamble: Optional FEAT-040.8 ``## Voice targets`` preamble
            (the user's measured voice as positive targets plus a
            personalised avoid-list) injected after the voice core.
    """

    def __init__(
        self,
        *,
        llm: DraftLLM | None = None,
        llm_factory: DraftLLMFactory | None = None,
        skills_root: Path,
        voice_core: str = "",
        style_preamble: str = "",
        privacy_override: PrivacyTierOverride | None = None,
        bypass_compiled: bool = False,
        ontology_twist: bool = False,
        embedding_fn: EmbeddingFn | None = None,
        grounding_thresholds: GroundingThresholds | None = None,
        fingerprint: VoiceFingerprint | None = None,
        ai_style_config: AIStyleConfig | None = None,
        voice_guard_no_llm: bool = False,
        cohesion: bool = False,
    ) -> None:
        """Initialise with an LLM client (or tier-keyed factory) and skill tree root.

        Exactly one of *llm* and *llm_factory* must be supplied.

        Args:
            llm: Callable ``(prompt) -> response`` for draft generation.
                Use this when the caller has already decided which provider
                the prompt may reach — ``creek_mcp.tools.draft`` derives its
                routing tier from the seed's sources *before* building the
                generator, and every test double comes in this way.
            llm_factory: Tier-keyed :data:`DraftLLMFactory` for callers that
                cannot know the tier yet (#1031). ``creek draft`` chooses its
                source fragments inside this class, so the CLI has nothing to
                key a client with at build time; passing the factory instead
                lets the generator call it once its sources are resolved and
                keep intimate bodies off a cloud provider.
            skills_root: Directory containing the SKILL.md subtree
                (``frequencies/``, ``phases/``, ``modes/``, ``registers/``).
            voice_core: Optional voice-core description the prompt will
                open with (e.g. a paragraph describing the human's
                baseline voice).
            style_preamble: Optional ``## Voice targets`` preamble built by
                :func:`creek.generate.ai_style.preamble.build_style_preamble`.
                Injected after the voice core in every prompt path (single,
                bare-section, and stitch) to steer generation toward the
                user's measured voice. Empty string (the default) omits it.
            privacy_override: Optional ``--include-tier`` value applied
                when scanning vault fragments for skill inference and
                source material gathering.
            bypass_compiled: When ``True`` the source-material gathering
                step skips the compiled layer and pulls fragments
                directly. Default ``False`` honours the FEAT-004
                compile-then-query discipline.
            ontology_twist: When ``True``, activate the twist
                composition path: compute source/target ontology
                profiles, name the divergent dimensions in the prompt,
                forbid verbatim paraphrase, and require ≥2 source
                fragments. Default ``False`` keeps the legacy
                composition path.
            embedding_fn: Optional paragraph embedding callable for the
                bidirectional grounding guard. When
                supplied, every generated draft is scored against its
                source fragments and the scores are written to the
                returned :class:`Draft` and the saved frontmatter. When
                ``None`` (the default) the guard is skipped — existing
                tests that construct a :class:`DraftGenerator` with a
                stub LLM continue to work without dragging in the
                sentence-transformer dependency.
            grounding_thresholds: Optional resolved guard thresholds.
                Required only when *embedding_fn* is supplied; ignored
                otherwise. Callers typically build this with
                :meth:`creek.generate.grounding.GroundingThresholds.from_config`.
            fingerprint: The user's voice fingerprint (FEAT-040.2). When
                supplied alongside *ai_style_config* the FEAT-040.9
                voice-fidelity guard runs at save time: sanitize, measure
                distance, and bounded-rewrite toward the user's voice. When
                ``None`` the guard is skipped.
            ai_style_config: The AI-style configuration. Required for the
                voice-fidelity guard; when ``None`` (or ``enabled`` is
                ``False``) the guard is skipped.
            voice_guard_no_llm: When ``True`` the voice-fidelity guard
                sanitizes and measures only — no rewrite hop (honours the
                ``creek draft --no-llm`` flag).
            cohesion: Opt-in switch for the no-fabrication cohesion pass.
                Default ``False`` so the single-topic draft
                path is byte-for-byte unchanged. When ``True`` — and the LLM
                is available (skipped under ``voice_guard_no_llm``) — a
                post-composition pass smooths seams with transitions, gated
                by the deterministic entity-preservation guard and a
                biographical-grounding re-check; any violation falls back to
                the pre-cohesion body.

        Raises:
            ValueError: When neither or both of *llm* and *llm_factory* are
                supplied. Defaulting either way would be worse than failing:
                with no client the generator cannot draft at all, and
                honouring one of two supplied seams would silently discard
                the caller's routing decision.
        """
        if llm is not None and llm_factory is not None:
            msg = "DraftGenerator takes llm or llm_factory, not both."
            raise ValueError(msg)
        if llm_factory is not None:
            self._llm_factory: DraftLLMFactory = llm_factory
            # Only a tier-keyed factory needs a tier, and deriving one costs a
            # vault survey per composition — so the survey is skipped entirely
            # for a pre-built client, whose caller already routed it.
            self._tier_keyed = True
        elif llm is not None:
            self._llm_factory = _fixed_llm_factory(llm)
            self._tier_keyed = False
        else:
            msg = "DraftGenerator requires one of llm or llm_factory."
            raise ValueError(msg)
        # Fail closed until sources are resolved (#1031): an unbound
        # generator routes as if its prompt carried intimate content, so a
        # path that forgets to bind cannot egress by omission.
        self._routing_tier = PrivacyTier.INTIMATE
        self.skills_root = skills_root
        self.voice_core = voice_core
        self.style_preamble = style_preamble
        self.privacy_override = privacy_override
        self.bypass_compiled = bypass_compiled
        self.ontology_twist = ontology_twist
        self._embedding_fn = embedding_fn
        self._grounding_thresholds = grounding_thresholds
        self._fingerprint = fingerprint
        self._ai_style_config = ai_style_config
        self._voice_guard_no_llm = voice_guard_no_llm
        self._cohesion = cohesion

    def present_idea(self, idea: IdeaSeed) -> str:
        """Format *idea* as an invitation rather than an imperative.

        The text mentions the idea's title, brief description, and the
        strategy that surfaced it, and gives the human the final say.

        Args:
            idea: The idea seed to present.

        Returns:
            A multi-line invitation string.
        """
        return (
            f'An idea has surfaced for you to consider: "{idea.title}".\n\n'
            f"{idea.brief_description}\n\n"
            f"(Surfaced via {idea.strategy.value.replace('_', ' ')}.) "
            "You may accept this invitation by drafting, or let it settle "
            "back into the compost."
        )

    def select_skill_stack(
        self,
        idea: IdeaSeed,
        *,
        vault_path: Path,
        current_phase: Phase | None = None,
        fragments: dict[str, tuple[Fragment, str]] | None = None,
    ) -> list[Path]:
        """Return the SKILL.md files to activate for *idea*.

        The stack is ordered: frequencies, phase, mode, register.
        Only files that exist under :attr:`skills_root` are returned;
        missing skills are skipped silently so the caller can operate
        on a partial skill tree.

        Args:
            idea: The idea seed whose attributes drive skill selection.
            vault_path: Vault root used to look up source fragments for
                dominant mode/register inference.
            current_phase: Optional current Archetypal Wavelength phase.
            fragments: Optional pre-loaded ``{id: (Fragment, body)}`` map;
                when provided the vault is not re-scanned.

        Returns:
            Ordered list of absolute paths to existing SKILL.md files.
        """
        paths: list[Path] = []
        paths.extend(self._frequency_skills(idea))
        phase_path = self._phase_skill(current_phase)
        if phase_path is not None:
            paths.append(phase_path)
        loaded = (
            fragments
            if fragments is not None
            else _load_fragments_by_id(
                vault_path / _FRAGMENTS_SUBDIR,
                privacy_override=self.privacy_override,
            )
        )
        source_frags = [
            loaded[fid][0] for fid in idea.source_fragments if fid in loaded
        ]
        mode_path = self._mode_skill(source_frags)
        if mode_path is not None:
            paths.append(mode_path)
        register_path = self._register_skill(source_frags)
        if register_path is not None:
            paths.append(register_path)
        return [p for p in paths if p.exists()]

    def _frequency_skills(self, idea: IdeaSeed) -> list[Path]:
        """Return the frequency SKILL.md paths declared by *idea*."""
        return [
            self.skills_root / _FREQUENCIES_DIR / f"{freq.value}{_SKILL_SUFFIX}"
            for freq in idea.frequency_affinity
        ]

    def _phase_skill(self, current_phase: Phase | None) -> Path | None:
        """Return the phase SKILL.md for *current_phase*, if classified."""
        if current_phase is None or current_phase == Phase.UNCLASSIFIED:
            return None
        return self.skills_root / _PHASES_DIR / f"{current_phase.value}{_SKILL_SUFFIX}"

    def _mode_skill(self, fragments: list[Fragment]) -> Path | None:
        """Return the mode-orientation SKILL.md dominant in *fragments*."""
        mode = _dominant_classified(f.wavelength.mode for f in fragments)
        orientation = _dominant_classified(f.wavelength.orientation for f in fragments)
        if mode is None or orientation is None:
            return None
        key = f"{mode}-{orientation}"
        return self.skills_root / _MODES_DIR / f"{key}{_SKILL_SUFFIX}"

    def _register_skill(self, fragments: list[Fragment]) -> Path | None:
        """Return the voice register SKILL.md dominant in *fragments*."""
        registers = [
            str(f.voice.voice_register)
            for f in fragments
            if f.voice.voice_register is not None
        ]
        register = _dominant([r for r in registers if r])
        if register is None:
            return None
        return self.skills_root / _REGISTERS_DIR / f"{register}{_SKILL_SUFFIX}"

    def gather_source_material(
        self,
        idea: IdeaSeed,
        *,
        vault_path: Path,
        fragments: dict[str, tuple[Fragment, str]] | None = None,
        compiled_pages: CompiledPageIndex | None = None,
        include_fragments: bool = True,
    ) -> str:
        """Collect compiled-layer summaries (compile-first) for *idea*.

        FEAT-004 routing: thread and eddy sections render the compiled
        page body when one exists; missing pages fall back to the
        frontmatter description and append a ``compile-needed`` entry
        to ``00-Creek-Meta/Processing-Log/compile-gaps.jsonl``. Source
        fragments are still pulled inline so drafts can quote them
        verbatim — provenance traversal stays available even when the
        compiled summary is the primary source.

        Args:
            idea: Idea seed whose IDs drive the lookup.
            vault_path: Vault root used to load fragments/threads/eddies.
            fragments: Optional pre-loaded ``{id: (Fragment, body)}`` map;
                when provided the vault is not re-scanned for fragments.
            compiled_pages: Optional pre-loaded compiled-page index. When
                ``None`` the index is loaded (or bypassed) per
                :attr:`bypass_compiled`.
            include_fragments: When ``True`` (default), render the
                ``## Source fragments`` block. The per-dimension path
                sets this ``False`` because the fragments
                are already laid out in the labelled per-dimension
                sections; double-rendering would amplify the heaviest
                ontology corner over the others.

        Returns:
            A newline-joined markdown string.
        """
        sections: list[str] = []
        loaded = (
            fragments
            if fragments is not None
            else _load_fragments_by_id(
                vault_path / _FRAGMENTS_SUBDIR,
                privacy_override=self.privacy_override,
            )
        )
        compiled = self._resolve_compiled_pages(vault_path, compiled_pages)
        if include_fragments:
            frag_block = _render_fragment_section(idea.source_fragments, loaded)
            if frag_block:
                sections.append(frag_block)
        thread_block = self._compose_thread_section(idea, vault_path, compiled)
        if thread_block:
            sections.append(thread_block)
        eddy_block = self._compose_eddy_section(idea, vault_path, compiled)
        if eddy_block:
            sections.append(eddy_block)
        return "\n\n".join(sections)

    def _resolve_compiled_pages(
        self,
        vault_path: Path,
        compiled_pages: CompiledPageIndex | None,
    ) -> CompiledPageIndex:
        """Return *compiled_pages* if provided, otherwise load it."""
        if compiled_pages is not None:
            return compiled_pages
        if self.bypass_compiled:
            return empty_index(bypassed=True)
        return load_compiled_pages(vault_path)

    def _compose_thread_section(
        self,
        idea: IdeaSeed,
        vault_path: Path,
        compiled: CompiledPageIndex,
    ) -> str:
        """Render the ``## Threads`` block, compile-first.

        The frontmatter scan ``_load_threads_by_id`` is deferred until a
        cache miss actually needs it, so fully-compiled vaults skip the
        disk walk entirely.
        """
        if not idea.threads:
            return ""
        threads: dict[str, Thread] | None = None
        entries: list[str] = []
        for tid in idea.threads:
            page = compiled.thread(tid)
            if page is not None:
                entries.append(_render_compiled_entry(tid, page))
                continue
            if not compiled.bypassed:
                log_compile_gap(
                    vault_path,
                    target_kind="thread",
                    target_id=tid,
                    surfaced_by="draft",
                    reason="missing",
                )
            if threads is None:
                threads = _load_threads_by_id(vault_path / _THREADS_SUBDIR)
            thread = threads.get(tid)
            if thread is not None:
                desc = thread.description.strip() or "(no description)"
                entries.append(f"### {tid}: {thread.title}\n{desc}")
        if not entries:
            return ""
        return "## Threads\n\n" + "\n\n".join(entries)

    def _compose_eddy_section(
        self,
        idea: IdeaSeed,
        vault_path: Path,
        compiled: CompiledPageIndex,
    ) -> str:
        """Render the ``## Eddies`` block, compile-first.

        The frontmatter scan ``_load_eddies_by_id`` is deferred until a
        cache miss actually needs it, so fully-compiled vaults skip the
        disk walk entirely.
        """
        if not idea.eddies:
            return ""
        eddies: dict[str, Eddy] | None = None
        entries: list[str] = []
        for eid in idea.eddies:
            page = compiled.eddy(eid)
            if page is not None:
                entries.append(_render_compiled_entry(eid, page))
                continue
            if not compiled.bypassed:
                log_compile_gap(
                    vault_path,
                    target_kind="eddy",
                    target_id=eid,
                    surfaced_by="draft",
                    reason="missing",
                )
            if eddies is None:
                eddies = _load_eddies_by_id(vault_path / _EDDIES_SUBDIR)
            eddy = eddies.get(eid)
            if eddy is not None:
                desc = eddy.description.strip() or "(no description)"
                entries.append(f"### {eid}: {eddy.title}\n{desc}")
        if not entries:
            return ""
        return "## Eddies\n\n" + "\n\n".join(entries)

    def _compose_prompt(
        self,
        idea: IdeaSeed,
        skill_stack: list[Path],
        source_material: str,
        *,
        per_dimension_material: str = "",
        twist_directive: str = "",
        twist_active: bool = False,
    ) -> str:
        """Assemble the LLM prompt from voice-core + skills + source + ask.

        When *per_dimension_material* is supplied, it is inserted ahead
        of the legacy ``## Source material`` block so the LLM sees the
        labelled per-dimension sections first and can weave them. Both
        blocks may co-exist — the per-dimension block carries the
        dimensional corpus slices, the legacy block still carries thread
        / eddy context for the synthetic IdeaSeed.

        When *twist_directive* is supplied, it is inserted immediately
        before the ``## Ask`` block so the LLM sees the recombination
        instruction as the last thing before its task.
        """
        parts: list[str] = [CARE_POLICY]
        if self.voice_core:
            parts.append(f"## Voice core\n{self.voice_core.strip()}")
        if self.style_preamble:
            parts.append(self.style_preamble)
        if skill_stack:
            skill_sections = [_render_skill_section(path) for path in skill_stack]
            parts.append("## Activated skills\n\n" + "\n\n".join(skill_sections))
        if per_dimension_material:
            parts.append(per_dimension_material)
        if source_material:
            parts.append(f"## Source material\n{source_material}")
        if twist_directive:
            parts.append(twist_directive)
        parts.append(
            _compose_ask_section(
                idea,
                per_dimension=bool(per_dimension_material),
                twist=twist_active,
            ),
        )
        return "\n\n".join(parts)

    def _build_guard_report(
        self,
        *,
        body: str,
        source_fragments: tuple[str, ...],
        fragments: dict[str, tuple[Fragment, str]],
    ) -> GroundingReport | None:
        """Run the grounding guard, or return ``None`` when disabled.

        The guard runs only when both an embedding callable and a
        :class:`GroundingThresholds` instance were supplied at
        construction time. Otherwise the method short-circuits — the
        legacy test suite constructs :class:`DraftGenerator` with a
        stub LLM only, and must keep working without dragging in the
        sentence-transformer dependency.

        The guard also prints a one-line summary to stderr so an
        operator running ``creek draft`` interactively sees the verdict
        before the saved-path message appears on stdout.

        Args:
            body: The generated draft body that just returned from the
                LLM.
            source_fragments: Fragment IDs that fed the draft prompt.
                Looked up in *fragments* to pull each source body for
                paragraph-level cosine scoring.
            fragments: Pre-loaded ``{id: (Fragment, body)}`` map (the
                same one the caller already loaded).

        Returns:
            A :class:`GroundingReport`, or ``None`` when the guard is
            not configured on this generator — or when the embedding
            model turned out to be unloadable at scoring time.
        """
        if self._embedding_fn is None or self._grounding_thresholds is None:
            return None
        source_texts = [
            fragments[fid][1] for fid in source_fragments if fid in fragments
        ]
        try:
            report = score_draft(
                body,
                source_texts=source_texts,
                embedding_fn=self._embedding_fn,
                thresholds=self._grounding_thresholds,
            )
            print(report.summary_line(), file=sys.stderr)
            # The non-``None`` invariant is established by the guard above;
            # assert it so ``_warn_ungrounded_biographical``'s non-optional
            # ``embedding_fn`` contract is explicit and mypy's narrowing is
            # pinned.
            assert self._embedding_fn is not None
            self._warn_ungrounded_biographical(
                body=body,
                source_texts=source_texts,
                embedding_fn=self._embedding_fn,
                thresholds=self._grounding_thresholds,
            )
        except EmbeddingModelUnavailableError as exc:
            self._disable_grounding(exc)
            return None
        return report

    def _disable_grounding(self, exc: EmbeddingModelUnavailableError) -> None:
        """Turn the grounding guard off for the rest of this run, loudly.

        The production ``embedding_fn`` loads a sentence-transformer on
        first call, so "the model is missing" surfaces mid-draft rather
        than at construction time. Losing the guard must not lose the
        draft: the generator drops back to its dormant behaviour (no
        scores stamped, no veto applied) and says so on stderr, mirroring
        :meth:`creek.author.agents.RetrievalAgent.retrieve`'s
        degrade-to-empty handling of the same typed error.

        Clearing both attributes also makes the failure sticky, so the
        later guard entry points short-circuit on their existing
        ``is None`` checks instead of re-raising the same load failure
        once per pass.

        Args:
            exc: The typed load failure, whose message already carries
                the model name and the operator's remediation.
        """
        self._embedding_fn = None
        self._grounding_thresholds = None
        print(f"grounding guard skipped: {exc}", file=sys.stderr)

    def _warn_ungrounded_biographical(
        self,
        *,
        body: str,
        source_texts: list[str],
        embedding_fn: EmbeddingFn,
        thresholds: GroundingThresholds,
    ) -> None:
        """Surface ungrounded first-person biographical sentences on stderr.

        Reuses the same cosine machinery as the paragraph guard but at
        sentence granularity (issue #515): a single invented first-person
        biographical claim — e.g. an upbringing the corpus never mentions —
        rides inside an otherwise on-topic paragraph and so escapes the
        paragraph-level grounding score. Each flagged sentence is printed as
        a ``grounding guard:`` warning so the operator sees it next to the
        paragraph verdict. The voice-core brief is added to the source corpus
        so a claim that legitimately traces to the brief is not flagged.

        Args:
            body: The generated draft body to scan.
            source_texts: Source-fragment bodies already resolved by the
                caller. The voice-core brief is appended internally.
            embedding_fn: The resolved (non-``None``) embedding callable the
                caller already established the guard is configured with.
            thresholds: The resolved grounding thresholds; its
                ``grounding_lower`` is the per-sentence floor.
        """
        corpus = source_texts.copy()
        if self.voice_core.strip():
            corpus.append(self.voice_core)
        findings = scan_biographical_sentences(
            body,
            source_texts=corpus,
            embedding_fn=embedding_fn,
            threshold=thresholds.grounding_lower,
        )
        for finding in findings:
            print(
                "grounding guard: ungrounded first-person biographical claim "
                f"(similarity {finding.max_similarity:.2f} < "
                f"{thresholds.grounding_lower:.2f}): {finding.sentence!r}",
                file=sys.stderr,
            )

    def generate_draft(
        self,
        idea: IdeaSeed,
        *,
        vault_path: Path,
        current_phase: Phase | None = None,
    ) -> Draft:
        """Compose a prompt and call the LLM to generate a :class:`Draft`.

        Args:
            idea: The idea seed to draft from.
            vault_path: Vault root used for skill-stack inference and
                source-material gathering.
            current_phase: Optional current phase for the phase skill.

        Returns:
            A populated :class:`Draft` — the body comes from the LLM
            callable; everything else is provenance.
        """
        fragments = _load_fragments_by_id(
            vault_path / _FRAGMENTS_SUBDIR,
            privacy_override=self.privacy_override,
        )
        compiled_pages = self._resolve_compiled_pages(vault_path, None)
        skill_stack = self.select_skill_stack(
            idea,
            vault_path=vault_path,
            current_phase=current_phase,
            fragments=fragments,
        )
        source_material = self.gather_source_material(
            idea,
            vault_path=vault_path,
            fragments=fragments,
            compiled_pages=compiled_pages,
        )
        prompt = self._compose_prompt(idea, skill_stack, source_material)
        # Before the first call that could reach a provider, and after the
        # sources are known (#1031). The cohesion and voice-guard hops below
        # inherit this tier — their inputs are distilled from the same
        # fragments.
        self._bind_routing_tier(vault_path, idea.source_fragments)
        body, truncated = self._invoke_draft_llm(
            prompt,
            empty_message="LLM returned an empty draft body",
        )
        body = self._apply_cohesion(
            body,
            source_fragments=idea.source_fragments,
            fragments=fragments,
        )
        guard = self._build_guard_report(
            body=body,
            source_fragments=idea.source_fragments,
            fragments=fragments,
        )
        return Draft(
            title=idea.title,
            body=body,
            idea_strategy=idea.strategy.value,
            source_fragments=idea.source_fragments,
            threads=idea.threads,
            eddies=idea.eddies,
            skill_stack=tuple(
                str(p.relative_to(self.skills_root)) for p in skill_stack
            ),
            prompt=prompt,
            generated_date=datetime.now(tz=UTC),
            derivative_score=guard.derivative_score if guard else None,
            grounding_score=guard.grounding_score if guard else None,
            paragraph_grounding=_guard_annotations(guard),
            truncated=truncated,
        )

    def _invoke_draft_llm(
        self,
        prompt: str,
        *,
        empty_message: str,
    ) -> tuple[str, bool]:
        """Call the draft LLM, returning ``(body, truncated)``.

        Normalises the legacy ``str`` return into a
        :class:`DraftLLMResponse`, strips the body, fails loudly on an
        empty body, and warns on stderr when the model stopped at the
        ``max_tokens`` ceiling.

        Args:
            prompt: The fully-composed prompt to send.
            empty_message: Error message for the :class:`RuntimeError`
                raised when the body is empty.

        Returns:
            A ``(body, truncated)`` tuple where *truncated* is ``True``
            when the stop reason was ``max_tokens``.

        Raises:
            RuntimeError: When the LLM returns an empty body.
        """
        response = _normalise_llm_response(
            self._llm_factory(self._routing_tier)(prompt)
        )
        body = response.text.strip()
        if not body:
            raise RuntimeError(empty_message)
        truncated = _warn_if_truncated(response.stop_reason)
        return body, truncated

    def _bind_routing_tier(
        self,
        vault_path: Path,
        source_ids: Sequence[str],
    ) -> PrivacyTier:
        """Key subsequent LLM calls to the tier *source_ids* carry (#1031).

        The draft twin of ``creek.compile.engine._routing_tier_for``, and it
        lives here for the same reason: the fragments a draft prompt renders
        are chosen inside this class, so this is the only layer that can name
        the tier the ``Intimate``-never-cloud chokepoint (#647) needs.

        The tier is derived from the sources this prompt actually renders —
        never from the whole admitted corpus. Under ``--include-tier all`` the
        loaded corpus contains every intimate fragment in the vault, so
        reducing over *that* would pin every such draft to the local model
        regardless of what the prompt says. (It is the same trap
        ``creek_mcp.tier_ceiling``'s ``ALL -> INTIMATE`` mapping sets for
        anyone tempted to reconcile a CLI "ceiling" here; the CLI operator is
        the vault owner and has no ceiling to reconcile.)

        Fail-closed asymmetry, mirroring
        ``creek_mcp.tools.draft._source_routing_tier``: ids that were named
        but did not resolve reduce to ``INTIMATE`` inside
        :func:`~creek.classify.privacy_filter.max_source_tier` — with no
        evidence about what a prompt carries, assume the worst. *No* ids named
        is a different statement: that prompt renders no vault fragment at all
        (an ontology-only seed, or an outline section that retrieved nothing),
        and failing it closed would force those drafts local for no privacy
        gain, so it routes as ``OPEN``.

        **Known gap, tracked separately.** The ``## Threads`` / ``## Eddies``
        blocks render *compiled-page* bodies, whose own sources are not
        surveyed here; the MCP twin closed that channel in #1013 via
        :func:`creek_mcp.tools.draft._compiled_source_ids`, and the CLI twin
        of that fix is issue #1538 rather than a widening of this one — the
        two surveys want to become one, which is a change to the shared
        privacy module, not to this call site.

        Cost: one vault survey per composition, and the outline path composes
        once per section. It is skipped entirely for a pre-built ``llm``,
        whose caller already routed it.

        Args:
            vault_path: Vault root; the survey reads ``01-Fragments``.
            source_ids: Ids of the fragments this prompt will render.

        Returns:
            The tier now bound — also stored for the calls that follow.
        """
        if not self._tier_keyed:
            return self._routing_tier
        if not source_ids:
            self._routing_tier = PrivacyTier.OPEN
        else:
            self._routing_tier = max_source_tier(
                source_tiers(vault_path, source_ids),
            )
        return self._routing_tier

    def _cohesion_grounding_check(
        self,
        body: str,
        *,
        source_fragments: tuple[str, ...],
        fragments: dict[str, tuple[Fragment, str]],
    ) -> Sequence[object]:
        """Re-run the biographical grounding flag on a cohesion-smoothed body.

        Used as the ``grounding_check`` seam for
        :func:`~creek.generate.cohesion.run_cohesion_pass` so the cohesion
        pass cannot smuggle in an ungrounded first-person biographical claim.
        Returns no findings — which lets the smoothed body through —
        when the grounding guard is not configured (no embedding callable),
        matching the dormant behaviour of the paragraph guard.

        Args:
            body: The cohesion-smoothed body to re-scan.
            source_fragments: Fragment IDs that fed the draft prompt.
            fragments: Pre-loaded ``{id: (Fragment, body)}`` map.

        Returns:
            One finding per ungrounded biographical sentence; empty when the
            guard is dormant, the embedding model is unloadable, or the body
            is clean.
        """
        if self._embedding_fn is None or self._grounding_thresholds is None:
            return []
        corpus = [fragments[fid][1] for fid in source_fragments if fid in fragments]
        if self.voice_core.strip():
            corpus.append(self.voice_core)
        try:
            return scan_biographical_sentences(
                body,
                source_texts=corpus,
                embedding_fn=self._embedding_fn,
                threshold=self._grounding_thresholds.grounding_lower,
            )
        except EmbeddingModelUnavailableError as exc:
            self._disable_grounding(exc)
            return []

    def _cohesion_llm(self, prompt: str) -> str:
        """Call the shared draft LLM for the cohesion hop, returning the body.

        Reuses the same client seam — and therefore the same routing tier
        (#1031) — so the cohesion pass cannot send a body distilled from
        intimate sources somewhere the composition prompt could not go. The
        :class:`DraftLLMResponse` wrapper is unwrapped to the plain body string
        the cohesion guard expects; a truncated stop reason is irrelevant here
        because the pass only adds transitions.

        Args:
            prompt: The cohesion prompt built by ``run_cohesion_pass``.

        Returns:
            The smoothed body text the LLM returned.
        """
        return _normalise_llm_response(
            self._llm_factory(self._routing_tier)(prompt),
        ).text

    def _apply_cohesion(
        self,
        body: str,
        *,
        source_fragments: tuple[str, ...],
        fragments: dict[str, tuple[Fragment, str]],
    ) -> str:
        """Run the opt-in cohesion pass, returning the smoothed or original body.

        **Single-topic only.** This is called from :meth:`generate_draft`,
        whose body is a single-topic essay. The cohesion pass must NOT be used
        on a multi-section outline draft (``compose_outline_draft``), which has
        its own content-frozen seam stitch; ``run_cohesion_pass`` additionally
        self-guards by no-opping on any body with 2+ top-level ``## `` headers.

        Default-off: when :attr:`_cohesion` is ``False`` or the
        draft is running under ``--no-llm`` (:attr:`_voice_guard_no_llm`),
        the body is returned unchanged and no extra LLM hop fires. Otherwise
        :func:`~creek.generate.cohesion.run_cohesion_pass` smooths the seams,
        gated by the entity-preservation guard and the biographical-grounding
        re-check; any violation falls back to *body*.

        Args:
            body: The composed single-topic draft body.
            source_fragments: Fragment IDs that fed the draft prompt; used to
                build the grounding re-check corpus.
            fragments: Pre-loaded ``{id: (Fragment, body)}`` map.

        Returns:
            The smoothed body when enabled and every guard passes; otherwise
            *body* unchanged.
        """
        enabled = self._cohesion and not self._voice_guard_no_llm
        cohesion_llm = self._cohesion_llm if enabled else None
        return run_cohesion_pass(
            body,
            cohesion_llm=cohesion_llm,
            enabled=enabled,
            grounding_check=lambda smoothed: self._cohesion_grounding_check(
                smoothed,
                source_fragments=source_fragments,
                fragments=fragments,
            ),
            voice_core=self.voice_core,
            bespoke_terms=_BESPOKE_ONTOLOGY_TERMS,
            capitalized_terms=_COMMON_CAPITALIZED_ONTOLOGY_TERMS,
        )

    def resolve_seed(
        self,
        spec: SeedSpec,
        *,
        vault_path: Path,
        fragments: dict[str, tuple[Fragment, str]] | None = None,
    ) -> IdeaSeed:
        """Build an :class:`IdeaSeed` from a manual *spec* (FEAT-032).

        Resolution rules:

        * ``spec.fragment_id`` set → load that one fragment and seed the
          idea with its title / threads / eddies / primary frequency.
          Missing IDs raise :class:`SeedResolutionError`.
        * Otherwise → filter every loaded fragment through
          :func:`_fragment_matches_dimensions` and, when supplied,
          ``spec.topic``. Zero matches raise
          :class:`SeedResolutionError`.

        Args:
            spec: The manual seed specification.
            vault_path: Vault root used to load fragments when
                *fragments* is not supplied.
            fragments: Optional pre-loaded ``{id: (Fragment, body)}``
                map; when given, the vault is not re-scanned.

        Returns:
            A synthetic :class:`IdeaSeed` whose dimensions reflect the
            user's request rather than a mined heuristic.

        Raises:
            SeedResolutionError: When *spec* is empty, the requested
                ``fragment_id`` is not in the vault, the dimensional /
                topic filters match zero fragments, or every active
                dimension produced an empty per-dimension slice.
        """
        if spec.is_empty:
            msg = "SeedSpec is empty — cannot resolve an idea seed"
            raise SeedResolutionError(msg)

        loaded = (
            fragments
            if fragments is not None
            else _load_fragments_by_id(
                vault_path / _FRAGMENTS_SUBDIR,
                privacy_override=self.privacy_override,
            )
        )

        if spec.fragment_id is not None:
            return self._seed_from_fragment_id(spec.fragment_id, loaded)

        slices = assemble_per_dimension_corpus(
            spec,
            loaded,
            ontology=spec.ontology,
            confidence_threshold=spec.ontology_confidence_threshold,
        )
        if slices:
            return self._seed_from_dimension_slices(spec, slices, loaded)

        # Topic-only path (no dimensions active) — legacy AND-semantics
        # collapses to a topic substring scan, which is exactly what we
        # still want when no ontology dimensions are in play.
        matched = self._matching_fragments(spec, loaded)
        if not matched:
            topic_part = f" topic {spec.topic!r}" if spec.topic else ""
            msg = (
                f"No source material matches{topic_part}. "
                "Try widening the filters or pouring in more sources."
            )
            raise SeedResolutionError(msg)

        return self._seed_from_matches(spec, matched)

    def _seed_from_dimension_slices(
        self,
        spec: SeedSpec,
        slices: dict[DimensionKey, DimensionSlice],
        loaded: dict[str, tuple[Fragment, str]],
    ) -> IdeaSeed:
        """Build an IdeaSeed from per-dimension slices (OR-semantics path).

        Emits a logger warning naming each empty dimension and proceeds
        whenever at least one slice has matches. Raises
        :class:`SeedResolutionError` when every dimension is empty so the
        CLI's existing error path prints a clear diagnostic.
        """
        try:
            raise_if_all_empty(slices)
        except AllDimensionsEmptyError as exc:
            raise SeedResolutionError(str(exc)) from exc
        for empty_key in empty_dimensions(slices):
            logger.warning(
                "Dimension %s matched zero fragments; proceeding with the others.",
                format_dimension_label(empty_key),
            )
        union_ids = union_fragment_ids(slices)
        matched = [loaded[fid] for fid in union_ids if fid in loaded]
        return self._seed_from_matches(spec, matched)

    def _seed_from_fragment_id(
        self,
        fragment_id: str,
        loaded: dict[str, tuple[Fragment, str]],
    ) -> IdeaSeed:
        """Resolve a single ``--seed-fragment`` request to an IdeaSeed."""
        from creek.generate.mining import IdeaSeed, MiningStrategy

        if fragment_id not in loaded:
            msg = (
                f"--seed-fragment {fragment_id!r} not found in vault "
                "(or excluded by the active privacy tier filter)."
            )
            raise SeedResolutionError(msg)
        fragment, _body = loaded[fragment_id]
        # ``use_enum_values=True`` persists each enum as its ``.value`` —
        # a bare string on disk — so coerce back via ``Frequency(str(...))``
        # before comparing against ``Frequency.UNCLASSIFIED``.
        primary = Frequency(str(fragment.frequency.primary))
        frequencies: tuple[Frequency, ...] = (
            (primary,) if primary != Frequency.UNCLASSIFIED else ()
        )
        return IdeaSeed(
            strategy=MiningStrategy.RESONANCE_CHAIN,
            title=fragment.title,
            source_fragments=(fragment_id,),
            threads=tuple(fragment.threads),
            eddies=tuple(fragment.eddies),
            frequency_affinity=frequencies,
            brief_description=(
                f"User-directed follow-up to fragment {fragment_id}: {fragment.title}."
            ),
            score=0.0,
        )

    def _matching_fragments(
        self,
        spec: SeedSpec,
        loaded: dict[str, tuple[Fragment, str]],
    ) -> list[tuple[Fragment, str]]:
        """Return ``(fragment, body)`` tuples that satisfy *spec*'s filters."""
        matched: list[tuple[Fragment, str]] = []
        for fragment, body in loaded.values():
            if not _fragment_matches_dimensions(
                fragment,
                frequencies=spec.frequencies,
                phases=spec.phases,
                modes=spec.modes,
            ):
                continue
            if spec.topic is not None and not _topic_matches_fragment(
                fragment, body, spec.topic
            ):
                continue
            matched.append((fragment, body))
        return matched

    def _seed_from_matches(
        self,
        spec: SeedSpec,
        matched: list[tuple[Fragment, str]],
    ) -> IdeaSeed:
        """Synthesise an IdeaSeed from a non-empty *matched* fragment list."""
        from creek.generate.mining import IdeaSeed, MiningStrategy

        fragments = [fragment for fragment, _ in matched]
        thread_ids, eddy_ids = _union_thread_and_eddy_ids(fragments)
        frequencies = spec.frequencies or _union_primary_frequencies(fragments)
        title, description = _seed_title_and_description(spec)
        return IdeaSeed(
            strategy=MiningStrategy.RESONANCE_CHAIN,
            title=title,
            source_fragments=tuple(fragment.id for fragment in fragments),
            threads=thread_ids,
            eddies=eddy_ids,
            frequency_affinity=frequencies,
            brief_description=description,
            score=0.0,
        )

    def generate_seeded_draft(
        self,
        spec: SeedSpec,
        *,
        vault_path: Path,
        current_phase: Phase | None = None,
    ) -> Draft:
        """Generate a :class:`Draft` from a manual *spec* (FEAT-032).

        Resolves the spec to a synthetic :class:`IdeaSeed`, runs the
        same skill-stack / source-material / prompt pipeline as
        :meth:`generate_draft`, then stamps the returned draft with
        the originating ``seed_spec`` for provenance.

        When the spec carries an explicit ``--seed-phase`` filter and
        the caller did not pass ``current_phase``, the spec's phase is
        used so the activated phase skill matches the user's request.

        When :attr:`ontology_twist` is enabled, the
        retrieved source material has its dominant ontology profile
        compared against the operator's target profile; divergent
        dimensions are named in a ``## Twist directive`` block and the
        ``## Ask`` is rewritten to forbid verbatim paraphrase. The
        helper :func:`require_plural_sources` raises
        :class:`PluralitySourceError` when fewer than two source
        fragments matched, since recombination requires more than one
        input.

        Args:
            spec: The manual seed specification.
            vault_path: Vault root used for fragment loading, skill
                resolution, and source-material gathering.
            current_phase: Optional phase override for skill selection.

        Returns:
            A :class:`Draft` whose ``seed_spec`` field records the
            originating manual seed and (when twist composition is
            active) whose ``source_profile`` / ``target_profile`` /
            ``twist_dimensions`` fields record the ontology delta.

        Raises:
            SeedResolutionError: Propagated from :meth:`resolve_seed`.
            PluralitySourceError: When twist composition is requested
                but fewer than two source fragments matched.
            RuntimeError: When the LLM returns an empty body.
        """
        fragments = _load_fragments_by_id(
            vault_path / _FRAGMENTS_SUBDIR,
            privacy_override=self.privacy_override,
        )
        idea = self.resolve_seed(spec, vault_path=vault_path, fragments=fragments)
        compiled_pages = self._resolve_compiled_pages(vault_path, None)
        effective_phase = current_phase
        if effective_phase is None and spec.phases:
            effective_phase = spec.phases[0]
        skill_stack = self.select_skill_stack(
            idea,
            vault_path=vault_path,
            current_phase=effective_phase,
            fragments=fragments,
        )
        slices = assemble_per_dimension_corpus(
            spec,
            fragments,
            ontology=spec.ontology,
            confidence_threshold=spec.ontology_confidence_threshold,
        )
        per_dimension_material = _render_per_dimension_sections(slices, fragments)
        twist_payload = self._build_twist_payload(spec, idea, fragments)
        source_material = self.gather_source_material(
            idea,
            vault_path=vault_path,
            fragments=fragments,
            compiled_pages=compiled_pages,
            include_fragments=not slices,
        )
        prompt = self._compose_prompt(
            idea,
            skill_stack,
            source_material,
            per_dimension_material=per_dimension_material,
            twist_directive=twist_payload.directive,
            twist_active=bool(twist_payload.dimensions),
        )
        # The per-dimension blocks render fragments the seed itself never
        # named, so the survey spans both sets (#1031).
        self._bind_routing_tier(
            vault_path,
            [*idea.source_fragments, *union_fragment_ids(slices)],
        )
        body, truncated = self._invoke_draft_llm(
            prompt,
            empty_message="LLM returned an empty draft body",
        )
        guard = self._build_guard_report(
            body=body,
            source_fragments=idea.source_fragments,
            fragments=fragments,
        )
        return Draft(
            title=idea.title,
            body=body,
            idea_strategy=idea.strategy.value,
            source_fragments=idea.source_fragments,
            threads=idea.threads,
            eddies=idea.eddies,
            skill_stack=tuple(
                str(p.relative_to(self.skills_root)) for p in skill_stack
            ),
            prompt=prompt,
            generated_date=datetime.now(tz=UTC),
            seed_spec=spec,
            per_dimension_sources=_slices_as_provenance(slices),
            source_profile=twist_payload.source_profile,
            target_profile=twist_payload.target_profile,
            twist_dimensions=twist_payload.dimensions,
            derivative_score=guard.derivative_score if guard else None,
            grounding_score=guard.grounding_score if guard else None,
            paragraph_grounding=_guard_annotations(guard),
            truncated=truncated,
        )

    def _build_twist_payload(
        self,
        spec: SeedSpec,
        idea: IdeaSeed,
        fragments: dict[str, tuple[Fragment, str]],
    ) -> _TwistPayload:
        """Build the ontology-twist payload for :meth:`generate_seeded_draft`.

        Returns an empty payload when :attr:`ontology_twist` is off so
        the legacy composition path is byte-for-byte unchanged.
        Otherwise enforces the plurality floor, computes source / target
        profiles, and renders the twist directive block.
        """
        if not self.ontology_twist:
            return _TwistPayload()
        require_plural_sources(idea.source_fragments)
        retrieved = [
            fragments[fid][0] for fid in idea.source_fragments if fid in fragments
        ]
        source_profile = source_profile_from_fragments(retrieved)
        target_profile = target_profile_from_spec(spec)
        divergent = twist_dimensions(source_profile, target_profile)
        directive = format_twist_directive(source_profile, target_profile, divergent)
        return _TwistPayload(
            directive=directive,
            source_profile=source_profile,
            target_profile=target_profile,
            dimensions=tuple(divergent),
        )

    def generate_outline_draft(
        self,
        outline_text: str,
        *,
        vault_path: Path,
        detect_ontology: OntologyDetector,
        current_phase: Phase | None = None,
    ) -> OutlineDraft:
        """Compose a multi-section draft from a markdown *outline_text*.

        Each outline section is composed independently — its ontology is
        detected, its source material is retrieved per dimension, and it
        is voiced through the ontology-twist path when at least two
        source fragments match. A final stitch pass smooths transitions
        between the sections without paraphrasing or inventing content
        (connective tissue only).

        Args:
            outline_text: The markdown outline (file contents or inline).
                Must contain at least one ATX header.
            vault_path: Vault root used for per-section fragment loading,
                skill resolution, and source-material gathering.
            detect_ontology: Callable mapping a section's seed text to a
                :class:`~creek.classify.prompt.PromptOntology`. Injected
                so the module stays provider-free; the CLI binds it to
                :func:`creek.classify.prompt.detect_ontology`.
            current_phase: Optional phase override applied to every
                section's skill selection.

        Returns:
            An :class:`OutlineDraft` whose ``sections`` carry per-section
            ontology profiles and whose ``body`` is the stitched essay.

        Raises:
            OutlineParseError: When *outline_text* has no markdown
                headers (surfaced unchanged from
                :func:`creek.generate.outline.parse_outline`).
            RuntimeError: When the stitch pass returns an empty body.
        """
        parsed = parse_outline(outline_text)
        fragments = _load_fragments_by_id(
            vault_path / _FRAGMENTS_SUBDIR,
            privacy_override=self.privacy_override,
        )
        composed: list[SectionComposition] = []
        section_tiers: list[PrivacyTier] = []
        for section in parsed:
            composed.append(
                self._compose_section(
                    section,
                    vault_path=vault_path,
                    detect_ontology=detect_ontology,
                    current_phase=current_phase,
                    fragments=fragments,
                ),
            )
            # Each section binds its own tier as it composes; collecting them
            # here is what lets the stitch below be keyed by the whole essay
            # rather than by whichever section happened to compose last.
            section_tiers.append(self._routing_tier)
        sections = tuple(composed)
        if self._tier_keyed:
            # The stitch prompt carries every section's body, so it routes at
            # the most sensitive tier any of them drew on (#1031). Guarded like
            # every other binding: a pre-built client was routed by its caller.
            self._routing_tier = max_source_tier(section_tiers)
        stitch_prompt = build_stitch_prompt(
            [(section.heading, section.body) for section in sections],
            voice_core=self.voice_core,
            voice_targets=self.style_preamble,
        )
        body, stitch_truncated = self._invoke_draft_llm(
            stitch_prompt,
            empty_message="LLM returned an empty stitched draft body",
        )
        truncated = stitch_truncated or any(section.truncated for section in sections)
        return OutlineDraft(
            title=parsed[0].heading,
            body=body,
            sections=sections,
            generated_date=datetime.now(tz=UTC),
            outline_text=outline_text,
            stitch_prompt=stitch_prompt,
            truncated=truncated,
        )

    def _compose_section(
        self,
        section: OutlineSection,
        *,
        vault_path: Path,
        detect_ontology: OntologyDetector,
        current_phase: Phase | None,
        fragments: dict[str, tuple[Fragment, str]],
    ) -> SectionComposition:
        """Detect, retrieve, and compose one outline *section*.

        The section's ontology is detected from its seed text and folded
        into a topic-plus-ontology :class:`SeedSpec`. When the spec
        resolves to source material the section is composed through the
        seeded-draft pipeline; otherwise it falls back to a bare
        skill-stack compose so the section still appears in the draft.
        The target profile is always recorded from the detected ontology.
        """
        ontology = detect_ontology(section.seed_text)
        spec = _section_seed_spec(section, ontology)
        target_profile = target_profile_from_spec(spec)
        try:
            idea = self.resolve_seed(spec, vault_path=vault_path, fragments=fragments)
        except SeedResolutionError:
            # A bare section renders no vault fragment at all — only the
            # operator's own outline text and the skill files (#1031).
            self._bind_routing_tier(vault_path, ())
            body, truncated = self._compose_bare_section(section, current_phase)
            return SectionComposition(
                heading=section.heading,
                level=section.level,
                body=body,
                target_profile=target_profile,
                truncated=truncated,
            )
        return self._compose_resolved_section(
            section,
            spec=spec,
            idea=idea,
            target_profile=target_profile,
            vault_path=vault_path,
            current_phase=current_phase,
            fragments=fragments,
        )

    def _compose_resolved_section(
        self,
        section: OutlineSection,
        *,
        spec: SeedSpec,
        idea: IdeaSeed,
        target_profile: OntologyProfile,
        vault_path: Path,
        current_phase: Phase | None,
        fragments: dict[str, tuple[Fragment, str]],
    ) -> SectionComposition:
        """Compose a section that resolved to source material.

        Builds the per-dimension prompt and (when at least two source
        fragments matched) the twist directive, then calls the LLM. A
        single-source section drops the twist directive rather than
        failing — a section is a sub-part of the essay, so the
        plurality floor is relaxed to keep the section in the draft.

        The section's source profile is aggregated once here and threaded
        into :meth:`_section_twist_payload` so the dominant-value
        aggregation is not recomputed when ontology-twist is active.
        """
        retrieved = [
            fragments[fid][0] for fid in idea.source_fragments if fid in fragments
        ]
        source_profile = source_profile_from_fragments(retrieved)
        twist = self._section_twist_payload(spec, idea, source_profile=source_profile)
        body, truncated = self._render_section_body(
            idea,
            spec=spec,
            twist=twist,
            vault_path=vault_path,
            current_phase=current_phase,
            fragments=fragments,
        )
        return SectionComposition(
            heading=section.heading,
            level=section.level,
            body=body,
            target_profile=target_profile,
            source_profile=source_profile,
            twist_dimensions=twist.dimensions,
            source_fragments=idea.source_fragments,
            truncated=truncated,
        )

    def _section_twist_payload(
        self,
        spec: SeedSpec,
        idea: IdeaSeed,
        *,
        source_profile: OntologyProfile,
    ) -> _TwistPayload:
        """Return the twist payload for a section, or empty when not applicable.

        Twist composition needs the flag enabled and at least two source
        fragments. A single-source section returns an empty payload so it
        still composes (the plurality floor is a hard error only on the
        single-topic ``generate_seeded_draft`` path).

        Args:
            spec: The section's seed specification.
            idea: The resolved idea seed for the section.
            source_profile: The section's source profile, aggregated once
                by the caller and threaded in to avoid recomputing the
                dominant-value aggregation.

        Returns:
            A populated :class:`_TwistPayload` when twist composition
            applies, otherwise the empty default payload.
        """
        if not self.ontology_twist or len(idea.source_fragments) < _MIN_TWIST_SOURCES:
            return _TwistPayload()
        target_profile = target_profile_from_spec(spec)
        divergent = twist_dimensions(source_profile, target_profile)
        directive = format_twist_directive(source_profile, target_profile, divergent)
        return _TwistPayload(
            directive=directive,
            source_profile=source_profile,
            target_profile=target_profile,
            dimensions=tuple(divergent),
        )

    def _render_section_body(
        self,
        idea: IdeaSeed,
        *,
        spec: SeedSpec,
        twist: _TwistPayload,
        vault_path: Path,
        current_phase: Phase | None,
        fragments: dict[str, tuple[Fragment, str]],
    ) -> tuple[str, bool]:
        """Build the per-section prompt and return ``(body, truncated)``."""
        effective_phase = current_phase
        if effective_phase is None and spec.phases:
            effective_phase = spec.phases[0]
        skill_stack = self.select_skill_stack(
            idea,
            vault_path=vault_path,
            current_phase=effective_phase,
            fragments=fragments,
        )
        slices = assemble_per_dimension_corpus(
            spec,
            fragments,
            ontology=spec.ontology,
            confidence_threshold=spec.ontology_confidence_threshold,
        )
        per_dimension_material = _render_per_dimension_sections(slices, fragments)
        source_material = self.gather_source_material(
            idea,
            vault_path=vault_path,
            fragments=fragments,
            include_fragments=not slices,
        )
        prompt = self._compose_prompt(
            idea,
            skill_stack,
            source_material,
            per_dimension_material=per_dimension_material,
            twist_directive=twist.directive,
            twist_active=bool(twist.dimensions),
        )
        # Per section, over exactly what this section's prompt renders (#1031).
        self._bind_routing_tier(
            vault_path,
            [*idea.source_fragments, *union_fragment_ids(slices)],
        )
        return self._invoke_section_llm(prompt)

    def _compose_bare_section(
        self,
        section: OutlineSection,
        current_phase: Phase | None,
    ) -> tuple[str, bool]:
        """Compose a source-less section, returning ``(body, truncated)``.

        The section still gets the voice core and (when classified) the
        phase skill, but the ask is built directly from the section's
        heading and body so the section appears in the draft even when
        the vault has nothing to retrieve.
        """
        parts: list[str] = [CARE_POLICY]
        if self.voice_core:
            parts.append(f"## Voice core\n{self.voice_core.strip()}")
        if self.style_preamble:
            parts.append(self.style_preamble)
        phase_path = self._phase_skill(current_phase)
        if phase_path is not None and phase_path.exists():
            parts.append("## Activated skills\n\n" + _render_skill_section(phase_path))
        parts.append(
            "## Ask\n"
            f'Write the section titled "{section.heading}". '
            f"{section.body.strip()} "
            "Write as the human — not as an AI. Honour the activated skills.",
        )
        return self._invoke_section_llm("\n\n".join(parts))

    def _invoke_section_llm(self, prompt: str) -> tuple[str, bool]:
        """Call the LLM for one section, returning ``(body, truncated)``.

        Fails loudly on an empty body and warns on stderr when the
        section was cut off at the ``max_tokens`` ceiling.
        """
        return self._invoke_draft_llm(
            prompt,
            empty_message="LLM returned an empty section body",
        )

    def _voice_rewrite_llm(self) -> Callable[[str], str]:
        """Adapt the draft LLM to the guard's ``(prompt) -> body`` signature."""

        def _rewrite(prompt: str) -> str:
            body, _ = self._invoke_draft_llm(
                prompt,
                empty_message="LLM returned an empty voice rewrite",
            )
            return body

        return _rewrite

    def _voice_grounding_check(
        self,
        vault_path: Path,
        source_fragments: tuple[str, ...],
    ) -> GroundingCheck | None:
        """Build the grounding veto, or ``None`` when grounding is not wired.

        The check re-scores a candidate rewrite against the same source
        fragments the grounding guard uses and rejects any rewrite that drops
        the draft below its grounding floor — so voice fidelity is never
        bought at the cost of grounding.
        """
        if not source_fragments:
            # Nothing to ground against (e.g. the outline-draft path): skip the
            # vault scan rather than build a trivially-passing no-op veto.
            return None
        if self._embedding_fn is None or self._grounding_thresholds is None:
            return None
        fragments = _load_fragments_by_id(
            vault_path / _FRAGMENTS_SUBDIR,
            privacy_override=self.privacy_override,
        )
        source_texts = tuple(
            fragments[fid][1] for fid in source_fragments if fid in fragments
        )
        embedding_fn = self._embedding_fn
        thresholds = self._grounding_thresholds

        def _still_grounded(body: str) -> bool:
            # No ``EmbeddingModelUnavailableError`` handling here, unlike the
            # two guard entry points that embed before this one. The veto is
            # built only from ``_apply_voice_fidelity``, which ``save_draft``
            # calls after ``generate_draft`` / ``generate_seeded_draft`` have
            # already run ``_build_guard_report``; the other caller,
            # ``save_outline_draft``, passes no source fragments and exits
            # above. So an unloadable model has always already tripped
            # ``_disable_grounding``, and the ``_embedding_fn is None`` check
            # above returns before this closure is ever built. Catching it
            # here again would be a branch nothing can reach.
            report = score_draft(
                body,
                source_texts=source_texts,
                embedding_fn=embedding_fn,
                thresholds=thresholds,
            )
            return not report.is_flagged_grounding

        return _still_grounded

    @staticmethod
    def _skipped_voice_guard(
        body: str,
        reason: str,
    ) -> tuple[str, dict[str, object]]:
        """Report a skipped de-slop pass loudly and stamp its reason.

        Args:
            body: The unchanged draft body.
            reason: A ``skipped:*`` :data:`VOICE_GUARD_STATUS_KEY` value.

        Returns:
            The unchanged *body* and a frontmatter payload carrying the reason.
        """
        print(f"voice-fidelity: skipped — {reason}", file=sys.stderr)
        return body, {VOICE_GUARD_STATUS_KEY: reason}

    def _apply_voice_fidelity(
        self,
        body: str,
        *,
        vault_path: Path,
        source_fragments: tuple[str, ...],
    ) -> tuple[str, dict[str, object]]:
        """Run the voice-fidelity guard, returning ``(body, frontmatter)``.

        Never a *silent* no-op: when the subsystem is disabled or the
        fingerprint is absent / thin, the body is returned unchanged but a
        loud ``voice_guard_status`` reason is stamped on the frontmatter and
        echoed to stderr. Otherwise it sanitizes, measures, and bounded-
        rewrites *body* toward the user's voice and returns the guarded body
        plus its frontmatter fields (which always carry a status).
        """
        config = self._ai_style_config
        fingerprint = self._fingerprint
        if config is None or not config.enabled:
            return self._skipped_voice_guard(body, "skipped:disabled")
        if fingerprint is None or fingerprint.fragment_count == 0:
            return self._skipped_voice_guard(body, "skipped:no_fingerprint")
        if fingerprint.is_thin(config.min_fingerprint_fragments):
            return self._skipped_voice_guard(body, "skipped:thin_fingerprint")
        report = run_voice_fidelity_guard(
            body,
            fingerprint=fingerprint,
            config=config,
            llm=self._voice_rewrite_llm(),
            no_llm=self._voice_guard_no_llm,
            style_preamble=self.style_preamble,
            grounding_check=self._voice_grounding_check(vault_path, source_fragments),
        )
        print(report.summary_line(), file=sys.stderr)
        return report.body, report.to_frontmatter()

    def save_outline_draft(
        self,
        draft: OutlineDraft,
        vault_path: Path,
    ) -> Path:
        """Write an :class:`OutlineDraft` to ``07-Voice/Drafts/``.

        The frontmatter records each section's heading, level, source
        fragments, and detected/target ontology profiles so a reader can
        trace every section back to its ontology and sources.

        The stitch prompt is recorded as a ``stitch_prompt_sha256``
        content hash rather than verbatim: the full prompt embeds every
        section body and can run to 10-15 KB, large enough to slow
        Obsidian's frontmatter parse. The verbatim outline input is still
        stored, so the prompt remains reproducible. A truncated draft is
        flagged with ``truncated: true`` and gets a footer in the body.

        Args:
            draft: The outline draft to persist.
            vault_path: Vault root under which ``07-Voice/Drafts`` lives.

        Returns:
            The absolute path of the written markdown file.
        """
        drafts_dir = vault_path / DRAFTS_SUBDIR
        drafts_dir.mkdir(parents=True, exist_ok=True)
        slug = _slugify(draft.title)
        date_prefix = draft.generated_date.strftime("%Y-%m-%d")
        target = _next_available_draft_path(drafts_dir, date_prefix, slug)
        body, voice_fields = self._apply_voice_fidelity(
            draft.body,
            vault_path=vault_path,
            source_fragments=(),
        )
        post = frontmatter.Post(
            content=_draft_file_body(body, truncated=draft.truncated),
            type="draft",
            title=draft.title,
            status="draft",
            idea_strategy="seed_outline",
            generated_date=draft.generated_date.isoformat(),
            outline=draft.outline_text,
            stitch_prompt_sha256=_stitch_prompt_digest(draft.stitch_prompt),
            truncated=draft.truncated,
            sections=[_serialise_section(section) for section in draft.sections],
        )
        for key, value in voice_fields.items():
            post[key] = value
        target.write_text(frontmatter.dumps(post), encoding="utf-8")
        return target

    def save_draft(self, draft: Draft, vault_path: Path) -> Path:
        """Write *draft* to ``07-Voice/Drafts/YYYY-MM-DD-{slug}.md``.

        The frontmatter captures every provenance field so the draft
        can be traced back to its sources and regenerated.

        Args:
            draft: The draft to persist.
            vault_path: Vault root under which ``07-Voice/Drafts`` lives.

        Returns:
            The absolute path of the written markdown file.
        """
        drafts_dir = vault_path / DRAFTS_SUBDIR
        drafts_dir.mkdir(parents=True, exist_ok=True)
        slug = _slugify(draft.title)
        date_prefix = draft.generated_date.strftime("%Y-%m-%d")
        target = _next_available_draft_path(drafts_dir, date_prefix, slug)
        body, voice_fields = self._apply_voice_fidelity(
            draft.body,
            vault_path=vault_path,
            source_fragments=draft.source_fragments,
        )
        post = frontmatter.Post(
            content=_draft_file_body(body, truncated=draft.truncated),
            type="draft",
            title=draft.title,
            status="draft",
            idea_strategy=draft.idea_strategy,
            source_fragments=list(draft.source_fragments),
            threads=list(draft.threads),
            eddies=list(draft.eddies),
            activated_skills=list(draft.skill_stack),
            generated_date=draft.generated_date.isoformat(),
            prompt=draft.prompt,
            truncated=draft.truncated,
        )
        if draft.seed_spec is not None:
            post["seed"] = _serialise_seed_spec(draft.seed_spec)
        if draft.per_dimension_sources:
            post["per_dimension_sources"] = _serialise_per_dimension_sources(
                draft.per_dimension_sources,
            )
        if draft.source_profile is not None:
            post["source_profile"] = draft.source_profile.as_mapping()
        if draft.target_profile is not None:
            post["target_profile"] = draft.target_profile.as_mapping()
        if draft.twist_dimensions:
            post["twist_dimensions"] = list(draft.twist_dimensions)
        guard_fields = build_grounding_frontmatter(
            derivative_score=draft.derivative_score,
            grounding_score=draft.grounding_score,
            paragraph_annotations=draft.paragraph_grounding,
        )
        for key, value in guard_fields.items():
            post[key] = value
        for key, value in voice_fields.items():
            post[key] = value
        target.write_text(frontmatter.dumps(post), encoding="utf-8")
        return target


def _union_thread_and_eddy_ids(
    fragments: list[Fragment],
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Return ``(thread_ids, eddy_ids)`` in first-seen order across *fragments*."""
    thread_ids: list[str] = []
    eddy_ids: list[str] = []
    for fragment in fragments:
        for tid in fragment.threads:
            if tid not in thread_ids:
                thread_ids.append(tid)
        for eid in fragment.eddies:
            if eid not in eddy_ids:
                eddy_ids.append(eid)
    return tuple(thread_ids), tuple(eddy_ids)


def _union_primary_frequencies(fragments: list[Fragment]) -> tuple[Frequency, ...]:
    """Return the ordered set of classified primary frequencies in *fragments*."""
    seen: list[Frequency] = []
    for fragment in fragments:
        # ``use_enum_values=True`` stores each enum as its ``.value`` on
        # disk; coerce back so equality / membership checks behave.
        primary = Frequency(str(fragment.frequency.primary))
        if primary == Frequency.UNCLASSIFIED:
            continue
        if primary not in seen:
            seen.append(primary)
    return tuple(seen)


def _seed_title_and_description(spec: SeedSpec) -> tuple[str, str]:
    """Return the synthetic IdeaSeed ``(title, description)`` for *spec*.

    The topic (when supplied) becomes the title; otherwise the title
    summarises the dimensional intersection. The description always
    captions the result as user-directed for downstream provenance.
    """
    descriptor = _seed_dimension_descriptor(spec)
    if spec.topic is not None:
        title = spec.topic
        if descriptor:
            description = (
                f"User-directed draft on {spec.topic!r}, drawing from "
                f"the {descriptor} corner of the vault."
            )
        else:
            description = f"User-directed draft on {spec.topic!r}."
        return title, description
    return (
        f"From the {descriptor} corner",
        f"User-directed draft from the {descriptor} corner of the vault.",
    )


def _serialise_seed_spec(spec: SeedSpec) -> dict[str, object]:
    """Render *spec* as a frontmatter-friendly mapping (FEAT-032 provenance).

    Only the populated fields are emitted so the YAML stays compact;
    a reader can re-run ``creek draft`` with the same flags by
    inspecting the recorded mapping.
    """
    payload: dict[str, object] = {}
    if spec.fragment_id is not None:
        payload["fragment_id"] = spec.fragment_id
    if spec.topic is not None:
        payload["topic"] = spec.topic
    if spec.frequencies:
        payload["frequencies"] = [freq.value for freq in spec.frequencies]
    if spec.phases:
        payload["phases"] = [ph.value for ph in spec.phases]
    if spec.modes:
        payload["modes"] = [md.value for md in spec.modes]
    return payload


def _section_seed_spec(
    section: OutlineSection,
    ontology: PromptOntology,
) -> SeedSpec:
    """Build a topic-plus-ontology :class:`SeedSpec` for one outline section.

    The section's seed text becomes the topic so the per-dimension
    retrieval and the legacy substring fallback both have a phrase to
    work with, and the detected ontology drives the dimensional blend.
    """
    return SeedSpec(topic=section.seed_text, ontology=ontology)


def _serialise_section(section: SectionComposition) -> dict[str, object]:
    """Render a :class:`SectionComposition` as frontmatter-friendly YAML.

    Records the section's heading, level, target ontology profile, and —
    when source material was found — its source profile, divergent
    twist dimensions, and source fragment IDs. The target profile is
    always present (the detector runs on every section), so each
    section's ontology profile is recorded in frontmatter. A truncated
    section is flagged with ``truncated: true``.
    """
    payload: dict[str, object] = {
        "heading": section.heading,
        "level": section.level,
        "target_profile": section.target_profile.as_mapping(),
    }
    if section.source_profile is not None:
        payload["source_profile"] = section.source_profile.as_mapping()
    if section.twist_dimensions:
        payload["twist_dimensions"] = list(section.twist_dimensions)
    if section.source_fragments:
        payload["source_fragments"] = list(section.source_fragments)
    if section.truncated:
        payload["truncated"] = True
    return payload


def _guard_annotations(
    report: GroundingReport | None,
) -> tuple[ParagraphAnnotation, ...]:
    """Return the per-paragraph annotation tuple for the :class:`Draft` field.

    Returns an empty tuple when *report* is ``None`` (guard disabled)
    so the dataclass default is preserved and existing tests that
    construct :class:`Draft` without the guard fields keep working.
    """
    if report is None:
        return ()
    return tuple(score.to_annotation() for score in report.paragraph_scores)


def _serialise_per_dimension_sources(
    entries: tuple[tuple[str, tuple[str, ...]], ...],
) -> dict[str, list[str]]:
    """Render per-dimension provenance as a frontmatter-friendly mapping.

    Each entry maps ``"kind:value"`` (e.g., ``"phase:peaking"``) to the
    ordered list of fragment IDs sourced from that dimension. Empty
    slices are omitted because the operator already has the explicit-
    flag echo in ``seed.frequencies`` / ``seed.phases`` / ``seed.modes``
    — the per-dimension mapping is specifically about *which fragments
    came from which dimension*.
    """
    return {label: list(ids) for label, ids in entries if ids}


def _render_per_dimension_sections(
    slices: dict[DimensionKey, DimensionSlice],
    fragments: dict[str, tuple[Fragment, str]],
) -> str:
    """Render the labelled ``## Material for ...`` blocks for the LLM prompt.

    Empty slices contribute a one-line ``(no source material)`` note
    so the model still sees that the dimension was attempted — the
    visible absence is part of the per-dimension warning surface.
    Non-empty slices render each matched fragment with its ID and title,
    just like the legacy ``## Source fragments`` block.
    """
    if not slices:
        return ""
    sections: list[str] = [
        _render_one_dimension_section(key, slice_, fragments)
        for key, slice_ in slices.items()
    ]
    return "\n\n".join(sections)


def _render_one_dimension_section(
    key: DimensionKey,
    slice_: DimensionSlice,
    fragments: dict[str, tuple[Fragment, str]],
) -> str:
    """Render one ``## Material for <label> (weight X.XX)`` block."""
    label = format_dimension_label(key)
    header = f"## Material for {label} (weight {slice_.weight:.2f})"
    if not slice_.fragment_ids:
        return f"{header}\n\n(no source material matched this dimension)"
    entries: list[str] = []
    for fid in slice_.fragment_ids:
        if fid not in fragments:
            continue
        fragment, body = fragments[fid]
        entries.append(f"### {fid}: {fragment.title}\n{body.strip()}")
    if not entries:
        return f"{header}\n\n(no source material matched this dimension)"
    return f"{header}\n\n" + "\n\n".join(entries)


def _slices_as_provenance(
    slices: dict[DimensionKey, DimensionSlice],
) -> tuple[tuple[str, tuple[str, ...]], ...]:
    """Flatten *slices* into hashable ``((label, ids), ...)`` for :class:`Draft`."""
    return tuple(
        (f"{key[0]}:{key[1]}", slice_.fragment_ids) for key, slice_ in slices.items()
    )


#: Prompt steer that forbids inventing biographical facts or another person's
#: motives (issue #515). Appended to every ``## Ask`` variant so the model is
#: told — regardless of mode — that it may connect ideas but never fabricate
#: the owner's upbringing, events, or someone else's motives. Pinned by a
#: structure test so the instruction can never silently drop out of the prompt.
_NO_FABRICATION_STEER = (
    "Do not assert biographical facts about me that are not present in the "
    "source fragments. You may draw connections between ideas, but never "
    "invent events, my upbringing, or another person's motives."
)

#: Prompt steer that forbids announcing provenance (issue #517). The model must
#: weave retrieved source fragments in as the owner's own present-tense thinking
#: rather than narrating that the prose came from a journal/note/entry or that
#: it is quoting itself. Appended to every ``## Ask`` variant and pinned by a
#: structure test so it can never silently drop out of the prompt.
_NO_PROVENANCE_STEER = (
    "Use the source fragments as your own voice and memory. Weave them in as "
    "your present-tense thinking — never reference that something came from a "
    "journal, note, or entry, and never narrate that you are quoting yourself. "
    "State the idea, not its provenance."
)

#: Prompt steer asking the model to gloss each bespoke ontology term in the
#: owner's voice on its first mention so a newcomer can follow. Appended to every
#: ``## Ask`` variant and pinned by a structure test so it can never silently
#: drop out of the prompt. Sourced from the glossary registry's
#: :data:`~creek.generate.ontology_glossary.GLOSS_STEER` so the draft and voice
#: surfaces carry identical wording.
_GLOSS_STEER = GLOSS_STEER


def _compose_ask_section(
    idea: IdeaSeed,
    *,
    per_dimension: bool,
    twist: bool = False,
) -> str:
    """Return the ``## Ask`` block, switching wording for per-dimension / twist mode.

    When per-dimension material is in play, the ask explicitly invites
    the model to weave across the labelled slices rather than treating
    them as one homogeneous corpus. When ontology twist is also on, the
    ask references the twist
    directive so the LLM treats it as load-bearing. Otherwise it keeps
    the original wording so existing tests and mined-idea drafts
    continue to compose identically.

    Every variant ends with :data:`_NO_FABRICATION_STEER`,
    :data:`_NO_PROVENANCE_STEER` and :data:`_GLOSS_STEER` so the draft prompt
    always carries the no-fabrication, weave-the-source-in, and
    gloss-bespoke-terms-on-first-mention instructions.
    """
    if twist:
        return (
            "## Ask\n"
            f'Write a draft essay titled "{idea.title}". '
            f"{idea.brief_description} "
            "Follow the twist directive above: use the source musings "
            "as ideas being explored, but voice them through the target "
            "combination's style. Do not paraphrase any single source "
            "verbatim — recombine across the corpus. Honour the "
            "activated skills. Write as the human — not as an AI. "
            "Cite source fragments by their IDs where relevant. "
            f"{_NO_FABRICATION_STEER} {_NO_PROVENANCE_STEER} {_GLOSS_STEER}"
        )
    if per_dimension:
        return (
            "## Ask\n"
            f'Write a draft essay titled "{idea.title}". '
            f"{idea.brief_description} "
            "Source material is presented in labelled per-dimension "
            "sections above; weave them together at composition time "
            "rather than treating any one section as the source of "
            "truth. Honour the activated skills. Write as the human — "
            "not as an AI. Cite source fragments by their IDs where "
            f"relevant. {_NO_FABRICATION_STEER} {_NO_PROVENANCE_STEER} "
            f"{_GLOSS_STEER}"
        )
    return (
        "## Ask\n"
        f'Write a draft essay titled "{idea.title}". '
        f"{idea.brief_description} "
        "Write as the human — not as an AI. Honour the activated "
        f"skills. Cite source fragments by their IDs where relevant. "
        f"{_NO_FABRICATION_STEER} {_NO_PROVENANCE_STEER} {_GLOSS_STEER}"
    )


def _render_compiled_entry(target_id: str, page: CompiledPage) -> str:
    """Return a ``### {id}: {title}`` block sourced from the compiled page.

    The body strips any leading ``# {title}`` heading (compile renders
    the title as the first line) so the ``###`` header isn't shadowed.
    """
    body = page.body.strip()
    lines = body.splitlines()
    if lines and lines[0].lstrip("# ").strip() == page.title.strip():
        body = "\n".join(lines[1:]).strip()
    if not body:
        body = "(compiled page has no body)"
    return f"### {target_id}: {page.title}\n{body}"


def _render_skill_section(path: Path) -> str:
    """Return a ``### {skill name}`` block with the file contents inlined.

    Args:
        path: Absolute path to the SKILL.md file to read.

    Returns:
        A markdown block with the skill heading and the file body; if the
        file cannot be read the block falls back to just the heading so
        the prompt still records which skill was activated.
    """
    try:
        body = path.read_text(encoding="utf-8").strip()
    except OSError:
        logger.warning("Could not read skill file %s; inlining name only.", path)
        return f"### {path.name}"
    if not body:
        return f"### {path.name}"
    return f"### {path.name}\n{body}"


def _render_fragment_section(
    ids: tuple[str, ...],
    fragments: dict[str, tuple[Fragment, str]],
) -> str:
    """Return the ``## Source fragments`` block for the given IDs."""
    entries: list[str] = []
    for fid in ids:
        if fid not in fragments:
            continue
        fragment, body = fragments[fid]
        entries.append(f"### {fid}: {fragment.title}\n{body.strip()}")
    if not entries:
        return ""
    return "## Source fragments\n\n" + "\n\n".join(entries)
