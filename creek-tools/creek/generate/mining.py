"""Blog idea mining — surface essay-worthy seeds from vault content.

Section 11.5 of the Creek Ontology defines four mining strategies that
convert raw vault activity into publishable-essay candidates:

1. *Liminal fragment x Eddy* — unnamed or compost fragments whose text
   echoes an established eddy (the fragment has resisted a thread but
   orbits a known cluster — an essay may want to name what is stirring).
2. *Thread terminus* — an active thread accumulates many fragments but
   never becomes a published essay (the thread is straining toward an
   argument that hasn't crystallised).
3. *Resonance chain* — three or more fragments linked by high-similarity
   synchronicities across distinct sources (a cross-source echo long
   enough to carry an essay).
4. *Wavelength-phase window* — the current phase of the Archetypal
   Wavelength surfaces fragments whose praxis potential aligns with that
   phase's voice (a seasonal essay prompt).

The miner is deterministic: it uses a Jaccard similarity over normalised
tokens by default so tests do not depend on embedding models. A caller
may inject any ``similarity_fn: Callable[[str, str], float]`` for more
sophisticated scoring.
"""

from __future__ import annotations

import logging
import operator
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, TypeVar

import frontmatter
import yaml
from pydantic import ValidationError

from creek.classify.privacy_filter import (
    PrivacyTierOverride,
    filter_fragments_by_tier,
)
from creek.generate.compile_routing import (
    COMPILE_GAPS_RELPATH,
    CompiledPageIndex,
    empty_index,
    load_compiled_pages,
    log_compile_gap,
)
from creek.models import (
    Eddy,
    Fragment,
    Phase,
    PraxisPotential,
    Thread,
    ThreadStatus,
)

if TYPE_CHECKING:
    from pathlib import Path

    from creek.models import Frequency


logger = logging.getLogger(__name__)

_FRAGMENTS_SUBDIR: str = "01-Fragments"
_THREADS_SUBDIR: str = "02-Threads"
_EDDIES_SUBDIR: str = "03-Eddies"
_ESSAYS_SUBDIR: str = "09-Reference/Published-Essays"
_LIMINAL_SUBDIR: str = "10-Liminal"
_SYNCHRONICITIES_SUBDIR: str = "10-Liminal/Synchronicities"

_ESSAY_MATCH_THRESHOLD: float = 0.5
"""Jaccard overlap above which a thread title is considered already-published."""


DEFAULT_SIMILARITY_LIMINAL: float = 0.35
"""Minimum Jaccard overlap between a liminal fragment and an eddy."""

DEFAULT_SIMILARITY_RESONANCE: float = 0.6
"""Minimum similarity for two fragments to share a resonance chain edge."""

DEFAULT_MIN_THREAD_FRAGMENTS: int = 10
"""Threads with strictly more fragments than this become terminus candidates."""

DEFAULT_MIN_CHAIN_LENGTH: int = 3
"""Minimum number of fragments in a resonance chain for it to surface."""

COMPILE_GAPS_LOG_RELPATH: Path = COMPILE_GAPS_RELPATH
"""Re-export of the canonical compile-gaps log path.

CLI callers and tests pinning the diagnostic wording import this symbol
instead of repeating the literal vault-relative string, keeping the
mining surface honest about its single source of truth.
"""


_NO_ACTIVE_THREADS: str = "no active threads available"
_NO_LIMINAL_FRAGMENTS: str = "no liminal fragments in 10-Liminal/{Unnamed,Compost}"
_NO_PHASE_MATCHES: str = "no fragments matched the current phase"
_NO_EXPLICIT_PRAXIS: str = "phase matches found but none had praxis_potential=EXPLICIT"
_NO_SYNCHRONICITIES: str = (
    "no synchronicity records on disk under 10-Liminal/Synchronicities; "
    "embeddings.parquet edges do not contribute until they are written as "
    "synchronicity notes (run `creek link` to populate)"
)
_UNCLASSIFIED_PHASE: str = "wavelength window skipped (current phase is unclassified)"


_STOPWORDS: frozenset[str] = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "but",
        "by",
        "for",
        "from",
        "has",
        "have",
        "i",
        "if",
        "in",
        "into",
        "is",
        "it",
        "its",
        "of",
        "on",
        "or",
        "that",
        "the",
        "their",
        "them",
        "this",
        "to",
        "was",
        "were",
        "will",
        "with",
        "you",
        "your",
    },
)


_TOKEN_RE = re.compile(r"[a-z]+")


def _tokens(text: str) -> set[str]:
    """Return the set of lowercase content tokens in *text*.

    Stopwords are removed before the set is built so that shared
    function words do not dominate the similarity score.
    """
    return {t for t in _TOKEN_RE.findall(text.lower()) if t not in _STOPWORDS}


def _jaccard_similarity(left: str, right: str) -> float:
    """Return the Jaccard similarity between *left* and *right*.

    Both strings are normalised to lowercase, split on non-alphabetic
    characters, and stripped of stopwords before the intersection-over-
    union is computed.

    Args:
        left: First text.
        right: Second text.

    Returns:
        A value in ``[0.0, 1.0]``. Returns ``0.0`` when either side has
        no content tokens.
    """
    left_tokens = _tokens(left)
    right_tokens = _tokens(right)
    if not left_tokens or not right_tokens:
        return 0.0
    intersection = left_tokens & right_tokens
    union = left_tokens | right_tokens
    return len(intersection) / len(union)


class MiningStrategy(StrEnum):
    """Which of the four Section 11.5 strategies produced an idea seed."""

    LIMINAL_CROSS_EDDY = "liminal_cross_eddy"
    THREAD_TERMINUS = "thread_terminus"
    RESONANCE_CHAIN = "resonance_chain"
    WAVELENGTH_WINDOW = "wavelength_window"


@dataclass(frozen=True)
class StrategyDiagnostic:
    """One strategy's per-run diagnostic counters (issue #340).

    Attributes:
        strategy: Which mining strategy this diagnostic describes.
        candidates_considered: How many distinct items the strategy
            examined before applying its filters (e.g. threads scanned,
            phase-matching fragments, components walked, liminal
            fragments considered).
        candidates_kept: How many of those items survived all filters
            and became seeds.
        top_score: Highest score observed across *candidates_considered*
            even when none were kept — lets the operator tell "almost
            kept" from "miles off".
        threshold: The dominant per-strategy threshold the score is
            compared against. Score and threshold share units so the
            CLI can present them side by side without conversion.
        fallback_reason: Human-readable reason a strategy had nothing
            to do (e.g. ``"no synchronicity records on disk"``), or
            ``None`` when the strategy ran normally.
    """

    strategy: MiningStrategy
    candidates_considered: int
    candidates_kept: int
    top_score: float
    threshold: float
    fallback_reason: str | None = None


@dataclass(frozen=True)
class MiningRunReport:
    """Snapshot of an :meth:`IdeaMiner.mine_all_with_report` invocation.

    Aggregates the seeds returned by :meth:`IdeaMiner.mine_all` with one
    :class:`StrategyDiagnostic` per strategy so the CLI can explain a
    zero-seed run without re-mining.

    ``top_score`` / ``top_threshold`` aggregates are intentionally
    omitted — the four strategies report scores in incompatible units
    (fragment counts vs. Jaccard similarity vs. a binary 1.0 gate) and
    ``max()`` across them is meaningless. Callers needing a summary
    must iterate ``diagnostics`` and surface each strategy's units
    side-by-side.
    """

    seeds: tuple[IdeaSeed, ...]
    diagnostics: tuple[StrategyDiagnostic, ...]


@dataclass(frozen=True)
class IdeaSeed:
    """An essay-worthy seed surfaced from the vault.

    Attributes:
        strategy: Which mining strategy generated the seed.
        title: Working title for the essay pitch.
        source_fragments: Fragment IDs that seeded the idea.
        threads: Thread IDs related to the seed.
        eddies: Eddy IDs related to the seed.
        frequency_affinity: APTITUDE frequencies the seed lives in.
        brief_description: One or two sentence pitch for the essay.
        score: Non-negative ranking score; higher is more promising.
    """

    strategy: MiningStrategy
    title: str
    source_fragments: tuple[str, ...]
    threads: tuple[str, ...]
    eddies: tuple[str, ...]
    frequency_affinity: tuple[Frequency, ...]
    brief_description: str
    score: float

    def __post_init__(self) -> None:
        """Validate ranking invariants."""
        if self.score < 0:
            msg = f"IdeaSeed.score must be non-negative, got {self.score!r}"
            raise ValueError(msg)


@dataclass(frozen=True)
class MiningSnapshot:
    """In-memory snapshot of the vault content the miner needs."""

    fragments: tuple[tuple[Fragment, str], ...] = ()
    liminal_fragments: tuple[tuple[Fragment, str, str], ...] = ()
    threads: tuple[Thread, ...] = ()
    eddies: tuple[Eddy, ...] = ()
    published_essay_titles: tuple[str, ...] = ()
    synchronicities: tuple[tuple[str, str, float], ...] = field(default_factory=tuple)
    compiled_pages: CompiledPageIndex = field(default_factory=empty_index)


def _safe_post(md_file: Path) -> frontmatter.Post | None:
    """Load a frontmatter post, returning ``None`` on parse errors."""
    try:
        return frontmatter.load(str(md_file))
    except (OSError, ValueError, yaml.YAMLError):
        logger.debug("Skipping unreadable markdown: %s", md_file)
        return None


def _validate_fragment(post: frontmatter.Post, path: Path) -> Fragment | None:
    """Validate a fragment post and log invalid records."""
    metadata = post.metadata.copy()
    if metadata.get("type") != "fragment":
        return None
    try:
        return Fragment.model_validate(metadata)
    except ValidationError:
        logger.debug("Skipping invalid fragment frontmatter: %s", path)
        return None


def _load_fragments(
    root: Path,
    *,
    privacy_override: PrivacyTierOverride | None = None,
) -> list[tuple[Fragment, str]]:
    """Load every fragment under *root* as ``(fragment, body)`` tuples.

    ``privacy_override`` is forwarded to
    :func:`~creek.classify.privacy_filter.filter_fragments_by_tier`; the
    default policy excludes intimate fragments and replaces personal
    bodies with title-only summaries.
    """
    if not root.exists():
        return []
    collected: list[tuple[Fragment, str]] = []
    for md_file in sorted(root.rglob("*.md")):
        post = _safe_post(md_file)
        if post is None:
            continue
        fragment = _validate_fragment(post, md_file)
        if fragment is not None:
            collected.append((fragment, post.content))
    return list(filter_fragments_by_tier(collected, override=privacy_override))


def _liminal_kind(md_file: Path, liminal_root: Path) -> str | None:
    """Return the immediate subfolder name (``Unnamed`` etc.), or ``None``.

    Returns ``None`` to signal "skip this file" — used for the
    ``synchronicities`` subfolder, whose records are reflection notes
    rather than fragments.
    """
    try:
        relative = md_file.relative_to(liminal_root)
    except ValueError:  # pragma: no cover - defensive
        return None
    kind = relative.parts[0] if relative.parts else ""
    if kind.lower() == "synchronicities":
        return None
    return kind


def _load_liminal_fragments(
    liminal_root: Path,
    *,
    privacy_override: PrivacyTierOverride | None = None,
) -> list[tuple[Fragment, str, str]]:
    """Return ``(fragment, body, kind)`` tuples for liminal fragments.

    *kind* is the immediate subfolder name under ``10-Liminal`` — typically
    ``"Unnamed"`` or ``"Compost"``. The Synchronicities subfolder is
    excluded because its records are reflection notes, not fragments.

    ``privacy_override`` is forwarded to the shared tier filter.
    """
    if not liminal_root.exists():
        return []
    collected: list[tuple[Fragment, str, str]] = []
    for md_file in sorted(liminal_root.rglob("*.md")):
        kind = _liminal_kind(md_file, liminal_root)
        if kind is None:
            continue
        post = _safe_post(md_file)
        if post is None:
            continue
        fragment = _validate_fragment(post, md_file)
        if fragment is not None:
            collected.append((fragment, post.content, kind))
    kind_by_id: dict[str, str] = {f.id: kind for f, _body, kind in collected}
    pairs = [(f, body) for f, body, _kind in collected]
    filtered = list(filter_fragments_by_tier(pairs, override=privacy_override))
    return [(f, body, kind_by_id[f.id]) for f, body in filtered]


_ModelT = TypeVar("_ModelT", Thread, Eddy)


def _load_typed(
    root: Path,
    *,
    type_tag: str,
    cls: type[_ModelT],
) -> list[_ModelT]:
    """Load validated model instances with the given ``type`` tag."""
    if not root.exists():
        return []
    collected: list[_ModelT] = []
    for md_file in sorted(root.rglob("*.md")):
        post = _safe_post(md_file)
        if post is None:
            continue
        metadata = dict(post.metadata)
        if metadata.get("type") != type_tag:
            continue
        try:
            model = cls.model_validate(metadata)
        except ValidationError:
            logger.debug("Skipping invalid %s frontmatter: %s", type_tag, md_file)
            continue
        collected.append(model)
    return collected


def _load_essay_titles(root: Path) -> list[str]:
    """Return the ``title`` frontmatter (falling back to filename) for essays."""
    if not root.exists():
        return []
    titles: list[str] = []
    for md_file in sorted(root.rglob("*.md")):
        post = _safe_post(md_file)
        if post is None:
            continue
        title = post.metadata.get("title")
        titles.append(str(title) if title else md_file.stem)
    return titles


def _load_synchronicities(root: Path) -> list[tuple[str, str, float]]:
    """Return ``(fragment_a_id, fragment_b_id, similarity)`` tuples."""
    if not root.exists():
        return []
    pairs: list[tuple[str, str, float]] = []
    for md_file in sorted(root.rglob("*.md")):
        post = _safe_post(md_file)
        if post is None:
            continue
        meta = post.metadata
        frag_a = meta.get("fragment_a_id")
        frag_b = meta.get("fragment_b_id")
        similarity = meta.get("similarity")
        if not (isinstance(frag_a, str) and isinstance(frag_b, str)):
            continue
        if not isinstance(similarity, (int, float)):
            continue
        pairs.append((frag_a, frag_b, float(similarity)))
    return pairs


def _load_mining_snapshot(
    vault_path: Path,
    *,
    privacy_override: PrivacyTierOverride | None = None,
    bypass_compiled: bool = False,
) -> MiningSnapshot:
    """Scan *vault_path* for every record the miner needs.

    The privacy override flows through to fragment loaders so callers
    cannot accidentally bypass tier filtering by reading the snapshot
    directly. When ``bypass_compiled`` is ``True`` the compiled-page
    index is constructed in bypass mode — every lookup misses without
    reading the compiled-layer subdirectories.
    """
    fragments = _load_fragments(
        vault_path / _FRAGMENTS_SUBDIR,
        privacy_override=privacy_override,
    )
    liminal = _load_liminal_fragments(
        vault_path / _LIMINAL_SUBDIR,
        privacy_override=privacy_override,
    )
    threads = _load_typed(
        vault_path / _THREADS_SUBDIR,
        type_tag="thread",
        cls=Thread,
    )
    eddies = _load_typed(
        vault_path / _EDDIES_SUBDIR,
        type_tag="eddy",
        cls=Eddy,
    )
    essay_titles = _load_essay_titles(vault_path / _ESSAYS_SUBDIR)
    syncs = _load_synchronicities(vault_path / _SYNCHRONICITIES_SUBDIR)
    compiled = (
        empty_index(bypassed=True)
        if bypass_compiled
        else load_compiled_pages(
            vault_path,
        )
    )
    return MiningSnapshot(
        fragments=tuple(fragments),
        liminal_fragments=tuple(liminal),
        threads=tuple(threads),
        eddies=tuple(eddies),
        published_essay_titles=tuple(essay_titles),
        synchronicities=tuple(syncs),
        compiled_pages=compiled,
    )


SimilarityFn = Callable[[str, str], float]


class IdeaMiner:
    """Surface essay-worthy seeds from the vault's accumulated state."""

    def __init__(
        self,
        *,
        similarity_fn: SimilarityFn | None = None,
        min_thread_fragments: int = DEFAULT_MIN_THREAD_FRAGMENTS,
        min_chain_length: int = DEFAULT_MIN_CHAIN_LENGTH,
        similarity_liminal: float = DEFAULT_SIMILARITY_LIMINAL,
        similarity_resonance: float = DEFAULT_SIMILARITY_RESONANCE,
        privacy_override: PrivacyTierOverride | None = None,
        bypass_compiled: bool = False,
    ) -> None:
        """Initialise with optional threshold and similarity overrides.

        Args:
            similarity_fn: A two-argument text similarity function in
                ``[0, 1]``. Defaults to :func:`_jaccard_similarity`.
            min_thread_fragments: Threads with strictly more fragments
                than this become terminus candidates.
            min_chain_length: Minimum number of fragments in a resonance
                chain to surface as a seed.
            similarity_liminal: Minimum similarity for a liminal fragment
                to anchor to an eddy.
            similarity_resonance: Minimum similarity for a resonance-chain
                edge.
            privacy_override: Optional ``--include-tier`` value. The
                default policy excludes intimate fragments and replaces
                personal bodies with title-only summaries.
            bypass_compiled: When ``True`` the miner skips the
                compiled-layer index entirely and reads fragments
                directly. Default ``False`` honours the FEAT-004
                compile-then-query discipline.
        """
        self._similarity_fn: SimilarityFn = similarity_fn or _jaccard_similarity
        self.min_thread_fragments = min_thread_fragments
        self.min_chain_length = min_chain_length
        self.similarity_liminal = similarity_liminal
        self.similarity_resonance = similarity_resonance
        self.privacy_override = privacy_override
        self.bypass_compiled = bypass_compiled

    def _resolve_snapshot(
        self,
        vault_path: Path,
        snapshot: MiningSnapshot | None,
    ) -> MiningSnapshot:
        """Return *snapshot* if provided, otherwise load from *vault_path*."""
        if snapshot is not None:
            return snapshot
        return _load_mining_snapshot(
            vault_path,
            privacy_override=self.privacy_override,
            bypass_compiled=self.bypass_compiled,
        )

    # ---- Strategy: Thread terminus ----

    def mine_thread_terminus(
        self,
        vault_path: Path,
        *,
        snapshot: MiningSnapshot | None = None,
    ) -> list[IdeaSeed]:
        """Return seeds for active threads that have not become essays.

        A thread is a *terminus candidate* when:

        * Its status is :attr:`ThreadStatus.ACTIVE`.
        * Its ``fragment_count`` is strictly greater than
          :attr:`min_thread_fragments`.
        * No published essay title substantially overlaps with the
          thread title.

        When a compiled thread page exists, ``source_fragments`` is
        derived from its provenance rather than rescanned from raw
        fragments. Missing pages append a ``compile-needed`` entry to
        ``compile-gaps.jsonl``.
        """
        seeds, _diag = self._mine_thread_terminus_with_diagnostic(
            vault_path,
            snapshot=snapshot,
        )
        return seeds

    def _mine_thread_terminus_with_diagnostic(
        self,
        vault_path: Path,
        *,
        snapshot: MiningSnapshot | None = None,
    ) -> tuple[list[IdeaSeed], StrategyDiagnostic]:
        """Run :meth:`mine_thread_terminus` and emit a diagnostic record.

        ``threshold`` is reported in raw fragment-count units so the
        diagnostic reads naturally ("top thread has 15 fragments,
        threshold is 10") rather than as a normalised ratio.
        """
        snap = self._resolve_snapshot(vault_path, snapshot)
        candidates = [
            thread
            for thread in snap.threads
            if _is_active(thread)
            and not self._has_matching_essay(
                thread.title,
                snap.published_essay_titles,
            )
        ]
        seeds = [
            self._seed_from_thread(
                thread,
                snap.fragments,
                snap.compiled_pages,
                vault_path,
            )
            for thread in candidates
            if thread.fragment_count > self.min_thread_fragments
        ]
        top_score = float(
            max((thread.fragment_count for thread in candidates), default=0),
        )
        diagnostic = StrategyDiagnostic(
            strategy=MiningStrategy.THREAD_TERMINUS,
            candidates_considered=len(candidates),
            candidates_kept=len(seeds),
            top_score=top_score,
            threshold=float(self.min_thread_fragments),
            fallback_reason=None if candidates else _NO_ACTIVE_THREADS,
        )
        _emit_diagnostic(diagnostic)
        return seeds, diagnostic

    def _has_matching_essay(
        self,
        thread_title: str,
        essay_titles: tuple[str, ...],
    ) -> bool:
        """Return ``True`` if any essay title echoes the thread title."""
        return any(
            self._similarity_fn(thread_title, essay_title) >= _ESSAY_MATCH_THRESHOLD
            for essay_title in essay_titles
        )

    def _seed_from_thread(
        self,
        thread: Thread,
        fragments: tuple[tuple[Fragment, str], ...],
        compiled: CompiledPageIndex,
        vault_path: Path,
    ) -> IdeaSeed:
        """Build an :class:`IdeaSeed` for a thread-terminus candidate.

        Source fragments are read from the compiled thread page's
        provenance when one exists; otherwise the miner falls back to
        scanning raw-fragment ``threads`` membership and records the
        gap so ``creek lint`` can surface it later.
        """
        compiled_thread = compiled.thread(thread.id)
        if compiled_thread is not None:
            linked = compiled.fragment_ids_for(compiled_thread)
        else:
            linked = tuple(
                frag.id for frag, _body in fragments if thread.id in frag.threads
            )
            if not compiled.bypassed:
                log_compile_gap(
                    vault_path,
                    target_kind="thread",
                    target_id=thread.id,
                    surfaced_by="mine.thread_terminus",
                    reason="missing",
                )
        description = (
            f"Thread '{thread.id}' has accumulated {thread.fragment_count} "
            "fragments without becoming a published essay. Its sustained "
            "return suggests an argument waiting to crystallise."
        )
        score = float(thread.fragment_count) / max(self.min_thread_fragments, 1)
        return IdeaSeed(
            strategy=MiningStrategy.THREAD_TERMINUS,
            title=thread.title,
            source_fragments=linked,
            threads=(thread.id,),
            eddies=(),
            frequency_affinity=tuple(thread.frequency_affinity),
            brief_description=description,
            score=score,
        )

    # ---- Combined ----

    def mine_all(
        self,
        vault_path: Path,
        *,
        current_phase: Phase,
        snapshot: MiningSnapshot | None = None,
    ) -> list[IdeaSeed]:
        """Run every strategy and return a deduped, score-ranked list.

        Args:
            vault_path: Root of the Obsidian vault.
            current_phase: The current Archetypal Wavelength phase.
                :attr:`Phase.UNCLASSIFIED` skips the wavelength strategy.
            snapshot: Optional pre-loaded snapshot to avoid re-scanning
                the vault between strategies.
        """
        return list(
            self.mine_all_with_report(
                vault_path,
                current_phase=current_phase,
                snapshot=snapshot,
            ).seeds,
        )

    def mine_all_with_report(
        self,
        vault_path: Path,
        *,
        current_phase: Phase,
        snapshot: MiningSnapshot | None = None,
    ) -> MiningRunReport:
        """Run every strategy and return seeds plus per-strategy diagnostics.

        This is the observability surface the CLI uses to explain a
        zero-seed run (issue #340). Each :class:`StrategyDiagnostic` in
        the returned :class:`MiningRunReport` reports candidates
        considered, candidates kept, the top score the strategy saw,
        the threshold the score is measured against, and an optional
        fallback reason.

        Args:
            vault_path: Root of the Obsidian vault.
            current_phase: The current Archetypal Wavelength phase.
                :attr:`Phase.UNCLASSIFIED` skips the wavelength strategy.
            snapshot: Optional pre-loaded snapshot to avoid re-scanning
                the vault between strategies.
        """
        snap = self._resolve_snapshot(vault_path, snapshot)
        thread_seeds, thread_diag = self._mine_thread_terminus_with_diagnostic(
            vault_path,
            snapshot=snap,
        )
        liminal_seeds, liminal_diag = self._mine_liminal_cross_eddy_with_diagnostic(
            vault_path,
            snapshot=snap,
        )
        wave_seeds, wave_diag = self._mine_wavelength_windows_with_diagnostic(
            vault_path,
            current_phase=current_phase,
            snapshot=snap,
        )
        chain_seeds, chain_diag = self._mine_resonance_chains_with_diagnostic(
            vault_path,
            snapshot=snap,
        )
        combined = thread_seeds + liminal_seeds + wave_seeds + chain_seeds
        return MiningRunReport(
            seeds=tuple(_dedupe_sorted_seeds(combined)),
            diagnostics=(thread_diag, liminal_diag, wave_diag, chain_diag),
        )

    # ---- Strategy: Resonance chain ----

    def mine_resonance_chains(
        self,
        vault_path: Path,
        *,
        snapshot: MiningSnapshot | None = None,
    ) -> list[IdeaSeed]:
        """Return seeds from long cross-source chains of resonant fragments.

        Synchronicity records are treated as edges. Connected components
        of size ``>= min_chain_length`` whose fragments span at least two
        distinct source platforms qualify. Edges with similarity below
        :attr:`similarity_resonance` are ignored.
        """
        seeds, _diag = self._mine_resonance_chains_with_diagnostic(
            vault_path,
            snapshot=snapshot,
        )
        return seeds

    def _mine_resonance_chains_with_diagnostic(
        self,
        vault_path: Path,
        *,
        snapshot: MiningSnapshot | None = None,
    ) -> tuple[list[IdeaSeed], StrategyDiagnostic]:
        """Run :meth:`mine_resonance_chains` and emit a diagnostic record.

        Reports the largest connected component size as ``top_score``
        and the chain-length floor as ``threshold`` so the operator
        sees the gap in raw fragment counts. When no synchronicity
        records exist on disk, the diagnostic carries a fallback
        reason and a ``compile-needed`` entry is appended.
        """
        snap = self._resolve_snapshot(vault_path, snapshot)
        qualified_edges = [
            (a, b)
            for a, b, score in snap.synchronicities
            if score >= self.similarity_resonance
        ]
        components = _connected_components(qualified_edges)
        fragments_by_id = {frag.id: frag for frag, _body in snap.fragments}
        seeds = [
            seed
            for seed in (
                self._chain_seed(component, fragments_by_id) for component in components
            )
            if seed is not None
        ]
        top_score = float(max((len(c) for c in components), default=0))
        fallback_reason = self._resonance_fallback_reason(
            snap.synchronicities,
            snap.compiled_pages,
            vault_path,
        )
        diagnostic = StrategyDiagnostic(
            strategy=MiningStrategy.RESONANCE_CHAIN,
            candidates_considered=len(components),
            candidates_kept=len(seeds),
            top_score=top_score,
            threshold=float(self.min_chain_length),
            fallback_reason=fallback_reason,
        )
        _emit_diagnostic(diagnostic)
        return seeds, diagnostic

    def _resonance_fallback_reason(
        self,
        synchronicities: tuple[tuple[str, str, float], ...],
        compiled: CompiledPageIndex,
        vault_path: Path,
    ) -> str | None:
        """Log + return a fallback reason when no synchronicities exist."""
        if synchronicities:
            return None
        if not compiled.bypassed:
            log_compile_gap(
                vault_path,
                target_kind="synchronicities",
                target_id="10-Liminal/Synchronicities",
                surfaced_by="mine.resonance_chain",
                reason="missing",
            )
        return _NO_SYNCHRONICITIES

    def _chain_seed(
        self,
        component: frozenset[str],
        fragments_by_id: dict[str, Fragment],
    ) -> IdeaSeed | None:
        """Return a chain seed for *component* when it qualifies."""
        if len(component) < self.min_chain_length:
            return None
        frags = [fragments_by_id[fid] for fid in component if fid in fragments_by_id]
        if len(frags) < self.min_chain_length:
            return None
        sources = {str(frag.source.platform) for frag in frags}
        if len(sources) < 2:
            return None

        sorted_ids = tuple(sorted(frag.id for frag in frags))
        frequencies = _union_frequencies(frags)
        first_title = next(
            (frag.title for frag in frags if frag.title), "Resonance chain"
        )
        description = (
            f"A cross-source echo of {len(frags)} fragments across "
            f"{len(sources)} platforms converges on a shared motif."
        )
        return IdeaSeed(
            strategy=MiningStrategy.RESONANCE_CHAIN,
            title=f"Echo chain: {first_title}",
            source_fragments=sorted_ids,
            threads=(),
            eddies=(),
            frequency_affinity=frequencies,
            brief_description=description,
            score=float(len(frags)) / self.min_chain_length,
        )

    # ---- Strategy: Wavelength-phase window ----

    def mine_wavelength_windows(
        self,
        vault_path: Path,
        *,
        current_phase: Phase,
        snapshot: MiningSnapshot | None = None,
    ) -> list[IdeaSeed]:
        """Return seeds surfacing fragments that match the current phase.

        A fragment becomes a seed when its :attr:`wavelength.phase`
        matches *current_phase* and its :attr:`praxis_potential` is
        :attr:`PraxisPotential.EXPLICIT`. Unclassified phases yield no
        seeds.

        Compile-first routing: when a compiled ``06-Frequencies/{F}``
        page exists for the fragment's primary frequency, only its
        provenance fragments surface; fragments outside that
        provenance are deferred until the index is recompiled. Missing
        frequency indexes log a ``compile-needed`` entry.
        """
        seeds, _diag = self._mine_wavelength_windows_with_diagnostic(
            vault_path,
            current_phase=current_phase,
            snapshot=snapshot,
        )
        return seeds

    def _mine_wavelength_windows_with_diagnostic(
        self,
        vault_path: Path,
        *,
        current_phase: Phase,
        snapshot: MiningSnapshot | None = None,
    ) -> tuple[list[IdeaSeed], StrategyDiagnostic]:
        """Run :meth:`mine_wavelength_windows` and emit a diagnostic record.

        The wavelength strategy is a binary phase + praxis gate, so
        ``top_score`` is reported as the number of phase-matching
        fragments and ``threshold`` is the minimum needed (1) for any
        seed to surface — making the gap visible without lying about
        per-candidate similarity.
        """
        if str(current_phase) == Phase.UNCLASSIFIED.value:
            diag = StrategyDiagnostic(
                strategy=MiningStrategy.WAVELENGTH_WINDOW,
                candidates_considered=0,
                candidates_kept=0,
                top_score=0.0,
                threshold=1.0,
                fallback_reason=_UNCLASSIFIED_PHASE,
            )
            _emit_diagnostic(diag)
            return [], diag
        snap = self._resolve_snapshot(vault_path, snapshot)
        phase_matches = [
            frag
            for frag, _body in snap.fragments
            if _matches_phase(frag, current_phase)
        ]
        gaps_logged: set[str] = set()
        seeds = [
            self._seed_from_phase_match(frag, current_phase)
            for frag in phase_matches
            if str(frag.praxis_potential) == PraxisPotential.EXPLICIT.value
            and self._frequency_admits_fragment(
                frag,
                snap.compiled_pages,
                vault_path,
                gaps_logged,
            )
        ]
        if not phase_matches:
            reason: str | None = _NO_PHASE_MATCHES
        elif not seeds:
            reason = _NO_EXPLICIT_PRAXIS
        else:
            reason = None
        diagnostic = StrategyDiagnostic(
            strategy=MiningStrategy.WAVELENGTH_WINDOW,
            candidates_considered=len(phase_matches),
            candidates_kept=len(seeds),
            # Pre-threshold peak — matches the pattern used by the other
            # three strategies. ``len(seeds)`` here would conflate "no
            # phase matches at all" with "all dropped by the praxis gate".
            # The praxis-gate-drop case is surfaced explicitly via
            # ``fallback_reason`` above so the per-strategy CLI breakdown
            # can name the real failure mode without operators having to
            # guess from a raw count.
            top_score=float(len(phase_matches)),
            threshold=1.0,
            fallback_reason=reason,
        )
        _emit_diagnostic(diagnostic)
        return seeds, diagnostic

    def _frequency_admits_fragment(
        self,
        fragment: Fragment,
        compiled: CompiledPageIndex,
        vault_path: Path,
        gaps_logged: set[str],
    ) -> bool:
        """Return ``True`` when *fragment* survives compile-first routing.

        - Compiled frequency-index page exists → fragment must appear in
          its provenance.
        - Compiled frequency-index page missing → log a gap once per
          frequency and admit the fragment (fragments-as-fallback).
        - Bypass mode → admit unconditionally without logging.
        """
        primary = str(fragment.frequency.primary)
        compiled_freq = compiled.frequency_index(primary)
        if compiled_freq is not None:
            return fragment.id in compiled.fragment_ids_for(compiled_freq)
        if compiled.bypassed:
            return True
        if primary not in gaps_logged:
            gaps_logged.add(primary)
            log_compile_gap(
                vault_path,
                target_kind="frequency_index",
                target_id=primary,
                surfaced_by="mine.wavelength_window",
                reason="missing",
            )
        return True

    def _seed_from_phase_match(
        self,
        fragment: Fragment,
        phase: Phase,
    ) -> IdeaSeed:
        """Build an :class:`IdeaSeed` for a wavelength window match."""
        description = (
            f"Fragment '{fragment.id}' surfaces during the {phase.value} "
            "phase and carries explicit praxis — a seasonal essay prompt."
        )
        return IdeaSeed(
            strategy=MiningStrategy.WAVELENGTH_WINDOW,
            title=fragment.title,
            source_fragments=(fragment.id,),
            threads=tuple(fragment.threads),
            eddies=tuple(fragment.eddies),
            frequency_affinity=_fragment_frequencies(fragment),
            brief_description=description,
            score=1.0,
        )

    # ---- Strategy: Liminal x Eddy ----

    def mine_liminal_cross_eddy(
        self,
        vault_path: Path,
        *,
        snapshot: MiningSnapshot | None = None,
    ) -> list[IdeaSeed]:
        """Return seeds pairing liminal fragments with resonant eddies.

        For every fragment under ``10-Liminal/{Unnamed,Compost}``, the
        best-scoring eddy above :attr:`similarity_liminal` becomes the
        anchor. The similarity score compares the liminal fragment's
        title + body text against the *compiled* eddy page's body when
        one exists, falling back to the eddy's frontmatter description
        and recording a ``compile-needed`` entry per missing eddy.
        """
        seeds, _diag = self._mine_liminal_cross_eddy_with_diagnostic(
            vault_path,
            snapshot=snapshot,
        )
        return seeds

    def _mine_liminal_cross_eddy_with_diagnostic(
        self,
        vault_path: Path,
        *,
        snapshot: MiningSnapshot | None = None,
    ) -> tuple[list[IdeaSeed], StrategyDiagnostic]:
        """Run :meth:`mine_liminal_cross_eddy` and emit a diagnostic record."""
        snap = self._resolve_snapshot(vault_path, snapshot)
        gaps_logged: set[str] = set()
        seeds: list[IdeaSeed] = []
        best_score = 0.0
        for fragment, body, _kind in snap.liminal_fragments:
            best = self._best_eddy_match_unfiltered(
                fragment,
                body,
                snap.eddies,
                snap.compiled_pages,
                vault_path,
                gaps_logged,
            )
            if best is None:
                continue
            eddy, score = best
            best_score = max(best_score, score)
            if score >= self.similarity_liminal:
                seeds.append(self._seed_from_liminal(fragment, eddy, score))
        reason = None if snap.liminal_fragments else _NO_LIMINAL_FRAGMENTS
        diagnostic = StrategyDiagnostic(
            strategy=MiningStrategy.LIMINAL_CROSS_EDDY,
            candidates_considered=len(snap.liminal_fragments),
            candidates_kept=len(seeds),
            top_score=best_score,
            threshold=self.similarity_liminal,
            fallback_reason=reason,
        )
        _emit_diagnostic(diagnostic)
        return seeds, diagnostic

    def _best_eddy_match(
        self,
        fragment: Fragment,
        body: str,
        eddies: tuple[Eddy, ...],
        compiled: CompiledPageIndex,
        vault_path: Path,
        gaps_logged: set[str],
    ) -> tuple[Eddy, float] | None:
        """Return the eddy with the highest similarity above threshold."""
        best = self._best_eddy_match_unfiltered(
            fragment,
            body,
            eddies,
            compiled,
            vault_path,
            gaps_logged,
        )
        if best is None or best[1] < self.similarity_liminal:
            return None
        return best

    def _best_eddy_match_unfiltered(
        self,
        fragment: Fragment,
        body: str,
        eddies: tuple[Eddy, ...],
        compiled: CompiledPageIndex,
        vault_path: Path,
        gaps_logged: set[str],
    ) -> tuple[Eddy, float] | None:
        """Return the highest-similarity eddy regardless of threshold.

        Used by :meth:`_mine_liminal_cross_eddy_with_diagnostic` so the
        diagnostic can surface the best score even when it failed to
        clear :attr:`similarity_liminal`. Returns ``None`` when *eddies*
        is empty.
        """
        frag_text = f"{fragment.title}\n{body}"
        scored: list[tuple[Eddy, float]] = []
        for eddy in eddies:
            eddy_text = self._eddy_compare_text(
                eddy,
                compiled,
                vault_path,
                gaps_logged,
            )
            scored.append((eddy, self._similarity_fn(frag_text, eddy_text)))
        if not scored:
            return None
        scored.sort(key=operator.itemgetter(1), reverse=True)
        return scored[0]

    def _eddy_compare_text(
        self,
        eddy: Eddy,
        compiled: CompiledPageIndex,
        vault_path: Path,
        gaps_logged: set[str],
    ) -> str:
        """Return the comparison text for *eddy*, compile-first.

        Uses the compiled eddy page's body when available; otherwise
        falls back to the eddy's frontmatter description and logs a
        compile-gap entry.

        ``gaps_logged`` is a per-run accumulator of eddy IDs whose gap
        has already been recorded; it ensures one entry per missing
        eddy regardless of how many liminal fragments visit it,
        mirroring the wavelength-window dedup contract.
        """
        compiled_eddy = compiled.eddy(eddy.id)
        if compiled_eddy is not None:
            return f"{compiled_eddy.title}\n{compiled_eddy.body}"
        if not compiled.bypassed and eddy.id not in gaps_logged:
            gaps_logged.add(eddy.id)
            log_compile_gap(
                vault_path,
                target_kind="eddy",
                target_id=eddy.id,
                surfaced_by="mine.liminal_cross_eddy",
                reason="missing",
            )
        return f"{eddy.title}\n{eddy.description}"

    def _seed_from_liminal(
        self,
        fragment: Fragment,
        eddy: Eddy,
        score: float,
    ) -> IdeaSeed:
        """Build an :class:`IdeaSeed` for a liminal x eddy resonance."""
        description = (
            f"Liminal fragment '{fragment.id}' echoes eddy '{eddy.id}'. "
            "Name what the fragment is trying to say about this cluster."
        )
        title = f"Naming what orbits '{eddy.title}'"
        return IdeaSeed(
            strategy=MiningStrategy.LIMINAL_CROSS_EDDY,
            title=title,
            source_fragments=(fragment.id,),
            threads=(),
            eddies=(eddy.id,),
            frequency_affinity=_fragment_frequencies(fragment),
            brief_description=description,
            score=score,
        )


def _emit_diagnostic(diagnostic: StrategyDiagnostic) -> None:
    """Log a one-line INFO record summarising one strategy's run.

    The shape (``[mine] <strategy>: ...``) is part of the operator
    contract — operators grep for it in pipeline logs to triage a
    zero-seed mining run (issue #340). Changing the prefix without
    updating downstream tooling will silently break that workflow.
    """
    reason = (
        f" reason={diagnostic.fallback_reason}" if diagnostic.fallback_reason else ""
    )
    logger.info(
        "[mine] %s: %d considered, %d kept (top_score=%.3f threshold=%.3f)%s",
        diagnostic.strategy.value,
        diagnostic.candidates_considered,
        diagnostic.candidates_kept,
        diagnostic.top_score,
        diagnostic.threshold,
        reason,
    )


def _dedupe_sorted_seeds(seeds: list[IdeaSeed]) -> list[IdeaSeed]:
    """Deduplicate *seeds* by identity, then sort by score desc / strategy / title."""
    deduped: list[IdeaSeed] = []
    seen: set[IdeaSeed] = set()
    for seed in seeds:
        if seed in seen:
            continue
        seen.add(seed)
        deduped.append(seed)
    deduped.sort(key=lambda s: (-s.score, s.strategy.value, s.title))
    return deduped


def _fragment_frequencies(fragment: Fragment) -> tuple[Frequency, ...]:
    """Return primary frequency plus any unique secondaries, in order.

    ``use_enum_values=True`` means the attributes are raw strings; callers
    still receive :class:`Frequency` values for type safety.
    """
    from creek.models import Frequency as _Freq  # local import keeps deps tight

    primary = _Freq(fragment.frequency.primary)
    result: list[Frequency] = [primary]
    seen = {primary}
    for secondary in fragment.frequency.secondary:
        freq = _Freq(secondary)
        if freq in seen:
            continue
        seen.add(freq)
        result.append(freq)
    return tuple(result)


def _connected_components(edges: list[tuple[str, str]]) -> list[frozenset[str]]:
    """Return connected components of the undirected graph defined by *edges*."""
    adjacency: dict[str, set[str]] = {}
    for a, b in edges:
        adjacency.setdefault(a, set()).add(b)
        adjacency.setdefault(b, set()).add(a)

    seen: set[str] = set()
    components: list[frozenset[str]] = []
    for node in adjacency:
        if node in seen:
            continue
        stack = [node]
        cluster: set[str] = set()
        while stack:
            current = stack.pop()
            if current in cluster:
                continue
            cluster.add(current)
            stack.extend(adjacency[current] - cluster)
        seen |= cluster
        components.append(frozenset(cluster))
    return components


def _union_frequencies(fragments: list[Fragment]) -> tuple[Frequency, ...]:
    """Return the ordered union of primary frequencies in *fragments*."""
    from creek.models import Frequency as _Freq

    seen: list[Frequency] = []
    seen_set: set[Frequency] = set()
    for fragment in fragments:
        freq = _Freq(fragment.frequency.primary)
        if freq in seen_set:
            continue
        seen_set.add(freq)
        seen.append(freq)
    return tuple(seen)


def _matches_phase(fragment: Fragment, phase: Phase) -> bool:
    """Return ``True`` when the fragment's wavelength phase equals *phase*."""
    return str(fragment.wavelength.phase) == phase.value


def _is_active(thread: Thread) -> bool:
    """Return ``True`` when *thread* has ``ACTIVE`` status.

    ``use_enum_values=True`` on the model means ``thread.status`` is the
    raw string value after validation, so we compare against
    :attr:`ThreadStatus.ACTIVE`'s value.
    """
    return str(thread.status) == ThreadStatus.ACTIVE.value


_PHASE_AWARE_STRATEGIES: dict[str, frozenset[MiningStrategy]] = {
    # Down phases prefer compost / synchronicity seeds; surfacing a
    # "draft your next essay" prompt during Bottoming Out misreads the
    # season. The ontology treats these phases as receptive.
    Phase.BOTTOMING_OUT.value: frozenset(
        {MiningStrategy.LIMINAL_CROSS_EDDY, MiningStrategy.RESONANCE_CHAIN},
    ),
    Phase.DIMINISHING.value: frozenset(
        {MiningStrategy.LIMINAL_CROSS_EDDY, MiningStrategy.RESONANCE_CHAIN},
    ),
    Phase.WITHDRAWAL.value: frozenset(
        {MiningStrategy.LIMINAL_CROSS_EDDY, MiningStrategy.RESONANCE_CHAIN},
    ),
    # Up phases prefer drafting / mining prompts that act on momentum.
    Phase.RISING.value: frozenset(
        {MiningStrategy.THREAD_TERMINUS, MiningStrategy.WAVELENGTH_WINDOW},
    ),
    Phase.PEAKING.value: frozenset(
        {MiningStrategy.THREAD_TERMINUS, MiningStrategy.WAVELENGTH_WINDOW},
    ),
    # Restoration is transitional; surface all four to mirror that.
    Phase.RESTORATION.value: frozenset(
        {
            MiningStrategy.LIMINAL_CROSS_EDDY,
            MiningStrategy.RESONANCE_CHAIN,
            MiningStrategy.THREAD_TERMINUS,
            MiningStrategy.WAVELENGTH_WINDOW,
        },
    ),
}
"""Which mining strategies surface for each phase (FEAT-007 pre-decided).

Bottoming Out / Diminishing / Withdrawal lean compost; Rising / Peaking
lean drafting; Restoration mixes both. Unclassified passes all four
through (handled in :func:`phase_filtered_seeds`).
"""

DEFAULT_PHASE_FILTERED_SEEDS: int = 5
"""Default cap on phase-filtered seeds (FEAT-007: 4-5 suggested questions)."""


def phase_filtered_seeds(
    vault_path: Path,
    phase: Phase | str,
    *,
    n: int = DEFAULT_PHASE_FILTERED_SEEDS,
    miner: IdeaMiner | None = None,
) -> list[IdeaSeed]:
    """Return up to *n* :class:`IdeaSeed` objects filtered for *phase*.

    The miner runs every strategy via :meth:`IdeaMiner.mine_all`, then
    keeps only the strategies appropriate for *phase* (see
    :data:`_PHASE_AWARE_STRATEGIES`). Unrecognised phases — including
    :attr:`Phase.UNCLASSIFIED` — fall through with no strategy filter:
    every seed competes on score.

    Args:
        vault_path: Root of the Obsidian vault.
        phase: Current wavelength phase. Accepts :class:`Phase` members
            or their string values for ergonomics from callers that
            already hold the raw string from frontmatter.
        n: Maximum number of seeds to return. Negative values clamp to
            zero.
        miner: Optional pre-configured :class:`IdeaMiner`. Defaults to
            a fresh instance with library defaults.

    Returns:
        Up to *n* seeds ordered by descending score, then strategy, then
        title — matching :meth:`IdeaMiner.mine_all`'s ordering.
    """
    phase_value = phase.value if isinstance(phase, Phase) else str(phase)
    if n <= 0:
        return []
    try:
        current_phase = Phase(phase_value)
    except ValueError:
        current_phase = Phase.UNCLASSIFIED
    active_miner = miner if miner is not None else IdeaMiner()
    all_seeds = active_miner.mine_all(vault_path, current_phase=current_phase)
    allowed = _PHASE_AWARE_STRATEGIES.get(phase_value)
    filtered = (
        all_seeds
        if allowed is None
        else [seed for seed in all_seeds if seed.strategy in allowed]
    )
    return filtered[:n]
