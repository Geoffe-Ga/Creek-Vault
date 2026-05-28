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
"""

from __future__ import annotations

import logging
import operator
import re
import sys
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import frontmatter
import yaml
from pydantic import ValidationError

from creek.classify.privacy_filter import (
    PrivacyTierOverride,
    filter_fragments_by_tier,
)
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
    score_draft,
)
from creek.generate.twist import (
    OntologyProfile,
    format_twist_directive,
    require_plural_sources,
    source_profile_from_fragments,
    target_profile_from_spec,
    twist_dimensions,
)
from creek.models import (
    CompiledPage,
    Eddy,
    Fragment,
    Frequency,
    Mode,
    Phase,
    Thread,
)

if TYPE_CHECKING:
    from pathlib import Path

    from creek.classify.prompt import PromptOntology
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


DraftLLM = Callable[[str], str]
"""Signature for draft LLM clients: takes a prompt, returns the draft body."""


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
            from prompt-level detection (issue #350). When set, its
            weighted dimensions augment the explicit-flag dimensions
            during per-dimension corpus retrieval (issue #351). An
            empty ontology is equivalent to ``None``.
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
        retrieval path (issue #351).
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
        per_dimension_sources: Per-dimension corpus attribution (issue
            #351). Maps a ``"kind:value"`` label (e.g.,
            ``"phase:peaking"``) to the tuple of fragment IDs that
            entered the LLM prompt as that dimension's labelled
            section. Empty when the draft used the legacy
            single-dimension path; populated whenever
            :func:`assemble_per_dimension_corpus` ran.
        source_profile: Ontology profile aggregated from the retrieved
            source fragments (issue #352). ``None`` when the draft did
            not pass through the twist composition path.
        target_profile: Ontology profile asked for by the operator
            (explicit flags + detected :class:`PromptOntology`). ``None``
            when the draft did not pass through the twist composition
            path.
        twist_dimensions: Ordered tuple of dimension names where the
            source profile diverges from the target profile (issue
            #352). Empty when the two profiles match (regression
            baseline) or when twist composition was not requested.
        derivative_score: Bidirectional grounding guard's derivative
            metric (issue #355). The maximum paragraph cosine
            similarity recorded between the draft body and every
            paragraph of the source fragments that fed the prompt.
            ``None`` when the guard did not run (no embedding callable
            wired) so the field reads as "not yet evaluated" rather
            than "evaluated clean".
        grounding_score: Bidirectional grounding guard's grounding
            metric (issue #355). The fraction of draft paragraphs whose
            ``max_similarity`` cleared
            :attr:`creek.config.DraftConfig.grounding_lower` against
            *some* source paragraph. ``None`` when the guard did not
            run.
        paragraph_grounding: Per-paragraph annotation list produced by
            :class:`creek.generate.grounding.GroundingReport`. Each
            entry carries ``{index, max_similarity, is_derivative,
            is_grounded}``. Empty when the guard did not run.
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
    """

    def __init__(
        self,
        *,
        llm: DraftLLM,
        skills_root: Path,
        voice_core: str = "",
        privacy_override: PrivacyTierOverride | None = None,
        bypass_compiled: bool = False,
        ontology_twist: bool = False,
        embedding_fn: EmbeddingFn | None = None,
        grounding_thresholds: GroundingThresholds | None = None,
    ) -> None:
        """Initialise with an LLM client and skill tree root.

        Args:
            llm: Callable ``(prompt) -> response`` for draft generation.
            skills_root: Directory containing the SKILL.md subtree
                (``frequencies/``, ``phases/``, ``modes/``, ``registers/``).
            voice_core: Optional voice-core description the prompt will
                open with (e.g. a paragraph describing the human's
                baseline voice).
            privacy_override: Optional ``--include-tier`` value applied
                when scanning vault fragments for skill inference and
                source material gathering.
            bypass_compiled: When ``True`` the source-material gathering
                step skips the compiled layer and pulls fragments
                directly. Default ``False`` honours the FEAT-004
                compile-then-query discipline.
            ontology_twist: When ``True``, activate the issue #352
                composition path: compute source/target ontology
                profiles, name the divergent dimensions in the prompt,
                forbid verbatim paraphrase, and require ≥2 source
                fragments. Default ``False`` keeps the legacy
                composition path (epic #349 gates the flag while in
                development).
            embedding_fn: Optional paragraph embedding callable for the
                issue #355 bidirectional grounding guard. When
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
        """
        self._llm = llm
        self.skills_root = skills_root
        self.voice_core = voice_core
        self.privacy_override = privacy_override
        self.bypass_compiled = bypass_compiled
        self.ontology_twist = ontology_twist
        self._embedding_fn = embedding_fn
        self._grounding_thresholds = grounding_thresholds

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
                (issue #351) sets this ``False`` because the fragments
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

        When *per_dimension_material* is supplied (issue #351), it is
        inserted ahead of the legacy ``## Source material`` block so the
        LLM sees the labelled per-dimension sections first and can weave
        them. Both blocks may co-exist — the per-dimension block carries
        the dimensional corpus slices, the legacy block still carries
        thread / eddy context for the synthetic IdeaSeed.

        When *twist_directive* is supplied (issue #352), it is inserted
        immediately before the ``## Ask`` block so the LLM sees the
        recombination instruction as the last thing before its task.
        """
        parts: list[str] = []
        if self.voice_core:
            parts.append(f"## Voice core\n{self.voice_core.strip()}")
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
        """Run the issue #355 grounding guard, or return ``None`` when disabled.

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
            not configured on this generator.
        """
        if self._embedding_fn is None or self._grounding_thresholds is None:
            return None
        source_texts = [
            fragments[fid][1] for fid in source_fragments if fid in fragments
        ]
        report = score_draft(
            body,
            source_texts=source_texts,
            embedding_fn=self._embedding_fn,
            thresholds=self._grounding_thresholds,
        )
        print(report.summary_line(), file=sys.stderr)
        return report

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
        body = self._llm(prompt).strip()
        if not body:
            msg = "LLM returned an empty draft body"
            raise RuntimeError(msg)
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
                dimension produced an empty per-dimension slice (issue
                #351).
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
        """Build an IdeaSeed from per-dimension slices (issue #351, OR path).

        Emits a logger warning naming each empty dimension and proceeds
        whenever at least one slice has matches, per the #351
        acceptance criterion. Raises :class:`SeedResolutionError` when
        every dimension is empty so the CLI's existing error path
        prints a clear diagnostic.
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

        When :attr:`ontology_twist` is enabled (issue #352), the
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
        body = self._llm(prompt).strip()
        if not body:
            msg = "LLM returned an empty draft body"
            raise RuntimeError(msg)
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
        post = frontmatter.Post(
            content=draft.body.strip() + "\n",
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
    came from which dimension*, the #351 acceptance criterion.
    """
    return {label: list(ids) for label, ids in entries if ids}


def _render_per_dimension_sections(
    slices: dict[DimensionKey, DimensionSlice],
    fragments: dict[str, tuple[Fragment, str]],
) -> str:
    """Render the labelled ``## Material for ...`` blocks for the LLM prompt.

    Empty slices contribute a one-line ``(no source material)`` note
    so the model still sees that the dimension was attempted — the
    visible absence is part of the per-dimension warning surface that
    the #351 issue asks for. Non-empty slices render each matched
    fragment with its ID and title, just like the legacy
    ``## Source fragments`` block.
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


def _compose_ask_section(
    idea: IdeaSeed,
    *,
    per_dimension: bool,
    twist: bool = False,
) -> str:
    """Return the ``## Ask`` block, switching wording for per-dimension / twist mode.

    When per-dimension material is in play (issue #351), the ask
    explicitly invites the model to weave across the labelled slices
    rather than treating them as one homogeneous corpus. When ontology
    twist is also on (issue #352), the ask references the twist
    directive so the LLM treats it as load-bearing. Otherwise it keeps
    the original wording so existing tests and mined-idea drafts
    continue to compose identically.
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
            "Cite source fragments by their IDs where relevant."
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
            "not as an AI. Cite source fragments by their IDs where relevant."
        )
    return (
        "## Ask\n"
        f'Write a draft essay titled "{idea.title}". '
        f"{idea.brief_description} "
        "Write as the human — not as an AI. Honour the activated "
        "skills. Cite source fragments by their IDs where relevant."
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


def _render_thread_section(
    ids: tuple[str, ...],
    threads: dict[str, Thread],
) -> str:
    """Return the ``## Threads`` block for the given IDs."""
    entries: list[str] = []
    for tid in ids:
        if tid not in threads:
            continue
        thread = threads[tid]
        desc = thread.description.strip() or "(no description)"
        entries.append(f"### {tid}: {thread.title}\n{desc}")
    if not entries:
        return ""
    return "## Threads\n\n" + "\n\n".join(entries)


def _render_eddy_section(
    ids: tuple[str, ...],
    eddies: dict[str, Eddy],
) -> str:
    """Return the ``## Eddies`` block for the given IDs."""
    entries: list[str] = []
    for eid in ids:
        if eid not in eddies:
            continue
        eddy = eddies[eid]
        desc = eddy.description.strip() or "(no description)"
        entries.append(f"### {eid}: {eddy.title}\n{desc}")
    if not entries:
        return ""
    return "## Eddies\n\n" + "\n\n".join(entries)
