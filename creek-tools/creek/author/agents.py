"""Specialist agents for the Creek Writing Desk.

The Graph, Retrieval, and Ontology specialists are real: Graph walks a
bounded backlink graph over the vault; Retrieval ranks fragments by semantic
similarity to the query, reusing :mod:`creek.link.embeddings`; Ontology runs
the deterministic rule classifier over the corpus and aggregates canonical
APTITUDE frequencies / phases / modes / dosages, surfacing — never resolving —
the paradoxes it finds. Every specialist returns structured,
provenance-tracked :class:`~creek.author.models.EvidenceBundle`s — claims paired
with their ``source_fragments`` (and an ``author_slug`` for borrowed evidence).
"""

from __future__ import annotations

import re
from collections import Counter
from typing import TYPE_CHECKING, Protocol, TypeVar, runtime_checkable

import numpy as np

from creek.author.models import (
    EvidenceBundle,
    EvidenceClaim,
    OntologyAnalysis,
    OntologyParadox,
    WalkStats,
)
from creek.classify.rules import RuleClassifier
from creek.classify.weighted import WeightedDimension
from creek.generate.paradox import OPPOSITE_CONFIDENCE_PAIRS, OPPOSITE_PHASE_PAIRS
from creek.link.embeddings import (
    EmbeddingLinker,
    EmbeddingModelUnavailableError,
    content_hash_for_text,
    embeddings_cache_path,
    fragment_embedding_text,
)
from creek.models import Confidence, Dosage, Frequency, Mode, Phase, VoiceRegister
from creek.vault.reader import iter_vault_fragments

if TYPE_CHECKING:
    from enum import StrEnum
    from pathlib import Path

    from creek.config import CreekConfig
    from creek.link.embeddings import CachedEmbedding
    from creek.models import Fragment

_DimT = TypeVar("_DimT", bound="StrEnum")

#: Vault subtrees the specialists draw evidence from.
_CORPUS_SUBDIRS: tuple[str, ...] = ("01-Fragments", "09-Reference", "11-Other-Authors")

#: Obsidian wikilink target, ignoring any ``|alias`` or ``#heading`` suffix.
_WIKILINK = re.compile(r"\[\[([^\]|#]+)")

#: Word characters used to score a seed against a query.
_WORD = re.compile(r"\w+")


@runtime_checkable
class Specialist(Protocol):
    """A specialist agent that contributes structured evidence.

    Implementations expose a stable :attr:`name` (used in the conductor's
    plan) and a :meth:`gather` method returning an :class:`EvidenceBundle`.
    """

    name: str

    def gather(self, query: str, vault: Path) -> EvidenceBundle:
        """Return structured evidence for *query* drawn from *vault*."""
        ...


def _load_config(vault: Path) -> CreekConfig:
    """Load the vault's config (defaults when no file is present)."""
    from creek.config import load_config, resolve_config_path

    return load_config(resolve_config_path(vault, None), warn_on_missing=False)


def _load_corpus(vault: Path) -> list[tuple[Fragment, str]]:
    """Return ``(fragment, body)`` records across the corpus subtrees."""
    records: list[tuple[Fragment, str]] = []
    for sub in _CORPUS_SUBDIRS:
        for _path, fragment, body, _meta in iter_vault_fragments(vault / sub):
            records.append((fragment, body))
    return records


def _fragment_claim(fragment: Fragment) -> EvidenceClaim:
    """Render a fragment as a structured claim, carrying its attribution."""
    return EvidenceClaim(
        claim=fragment.title,
        source_fragments=[fragment.id],
        author_slug=fragment.source.author_slug,
    )


def _cosine(left: list[float], right: list[float]) -> float:
    """Return the cosine similarity of two vectors (``0.0`` if degenerate)."""
    a = np.asarray(left, dtype=float)
    b = np.asarray(right, dtype=float)
    denom = float(np.linalg.norm(a) * np.linalg.norm(b))
    return float(a @ b / denom) if denom else 0.0


def _fragment_vector(
    fragment: Fragment,
    linker: EmbeddingLinker,
    cache: dict[str, CachedEmbedding],
) -> list[float]:
    """Return *fragment*'s embedding, reusing a fresh cached vector when present.

    A cached vector is trusted only when the fragment id is in *cache* and its
    stored ``content_hash`` still equals the SHA-256 of the fragment's current
    :func:`fragment_embedding_text` — the exact freshness key
    :meth:`EmbeddingLinker.build_cache_entries` writes. A miss or a stale hash
    falls back to a live :meth:`EmbeddingLinker.generate_embedding`, so the
    result is identical to embedding from scratch.

    Args:
        fragment: The corpus fragment to embed.
        linker: The reused embedding linker (live-embed fallback).
        cache: Persisted, model-filtered cache keyed by fragment id.

    Returns:
        The fragment's embedding vector.
    """
    text = fragment_embedding_text(fragment)
    cached = cache.get(fragment.id)
    if cached is not None and cached.content_hash == content_hash_for_text(text):
        return cached.vector
    return linker.generate_embedding(text)


def _rank_fragments(
    query: str,
    corpus: list[tuple[Fragment, str]],
    linker: EmbeddingLinker,
    cache: dict[str, CachedEmbedding],
) -> list[Fragment]:
    """Rank *corpus* fragments by semantic similarity to *query* (descending).

    Embeds the query once with the reused *linker* and scores each fragment
    against it, drawing fresh cached vectors from *cache* to avoid re-embedding
    unchanged fragments (the cache is a pure optimisation — a hit yields the
    same vector a live embed would). Ties break by fragment id for determinism.
    Raises :class:`EmbeddingModelUnavailableError` when the embedding model
    cannot load.

    Args:
        query: The user query to rank fragments against.
        corpus: ``(fragment, body)`` records to score.
        linker: The reused embedding linker.
        cache: Persisted, model-filtered embedding cache keyed by fragment id.

    Returns:
        Fragments ordered by cosine similarity descending, id tie-break.
    """
    query_vec = linker.generate_embedding(query)
    scored: list[tuple[float, str, Fragment]] = []
    for fragment, _body in corpus:
        vec = _fragment_vector(fragment, linker, cache)
        scored.append((_cosine(query_vec, vec), fragment.id, fragment))
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [fragment for _score, _id, fragment in scored]


class RetrievalSpecialist:
    """Semantic-retrieval specialist over raw, reference, and other-author text.

    Holds a lazily-created :class:`EmbeddingLinker` on the instance so a
    long-lived specialist reused across :meth:`gather` calls loads the
    sentence-transformer model only once. The linker is instance-level (never a
    module global) so a fresh ``RetrievalSpecialist()`` per test starts cold and
    the conftest model mock cannot leak across tests.
    """

    name = "retrieval"

    def __init__(self, *, linker: EmbeddingLinker | None = None) -> None:
        """Initialise the specialist with an optional pre-built linker.

        Args:
            linker: An embedding linker to reuse; when ``None`` (the default,
                as in :func:`default_specialists`) one is created lazily on
                first :meth:`gather` and cached on the instance.
        """
        self._linker = linker
        self._cache: dict[str, CachedEmbedding] | None = None
        self._cache_vault: Path | None = None

    def _get_linker(self, config: CreekConfig) -> EmbeddingLinker:
        """Return the instance's linker, creating and caching it on first use.

        Args:
            config: The loaded vault config supplying embeddings settings.

        Returns:
            The reused :class:`EmbeddingLinker` for this specialist instance.
        """
        if self._linker is None:
            self._linker = EmbeddingLinker(config.embeddings)
        return self._linker

    def _get_cache(
        self,
        linker: EmbeddingLinker,
        vault: Path,
    ) -> dict[str, CachedEmbedding]:
        """Return the persisted embeddings cache, read once per instance + vault.

        The parquet is loaded on first use for a given vault and reused across
        later :meth:`gather` calls so the desk does not re-read it every call.
        Staleness is harmless: ``_fragment_vector`` validates each entry's
        ``content_hash`` against the fragment's current text, so a fragment
        re-embedded into the parquet after this instance cached the dict simply
        falls back to a live embed — never a wrong vector. A different *vault*
        reloads.

        Args:
            linker: The reused linker whose ``load_cache`` reads the parquet.
            vault: The vault whose embeddings cache is read.

        Returns:
            The fragment-id keyed embedding cache (empty when absent).
        """
        if self._cache is None or self._cache_vault != vault:
            self._cache = linker.load_cache(embeddings_cache_path(vault))
            self._cache_vault = vault
        return self._cache

    def gather(self, query: str, vault: Path) -> EvidenceBundle:
        """Return the top-``retrieval_top_k`` fragments most relevant to *query*.

        Degrades to an empty bundle when the corpus is empty or the embedding
        model is unavailable, so the desk never crashes on a thin/offline vault.
        The persisted embeddings cache is loaded once and used to skip
        re-embedding fragments whose text is unchanged.
        """
        config = _load_config(vault)
        corpus = _load_corpus(vault)
        if not corpus:
            return EvidenceBundle()
        linker = self._get_linker(config)
        cache = self._get_cache(linker, vault)
        try:
            ranked = _rank_fragments(query, corpus, linker, cache)
        except EmbeddingModelUnavailableError:
            return EvidenceBundle()
        top = ranked[: config.author.retrieval_top_k]
        return EvidenceBundle(claims=[_fragment_claim(fragment) for fragment in top])


def _resolve_seed(query: str, corpus: list[tuple[Fragment, str]]) -> str:
    """Pick the seed fragment whose title best matches *query* (id tie-break)."""
    words = {match.group(0).lower() for match in _WORD.finditer(query)}
    best_id = ""
    best_score = -1
    for fragment, _body in sorted(corpus, key=lambda item: item[0].id):
        title_words = {m.group(0).lower() for m in _WORD.finditer(fragment.title)}
        score = len(words & title_words)
        if score > best_score:
            best_score, best_id = score, fragment.id
    return best_id


def _wikilink_targets(
    body: str,
    by_id: set[str],
    by_title: dict[str, str],
) -> set[str]:
    """Resolve a body's ``[[wikilink]]`` targets to known fragment ids."""
    resolved: set[str] = set()
    for match in _WIKILINK.finditer(body):
        target = match.group(1).strip()
        fid = target if target in by_id else by_title.get(target)
        if fid:
            resolved.add(fid)
    return resolved


def _resolve_titles(corpus: list[tuple[Fragment, str]]) -> dict[str, str]:
    """Map each *unambiguous* title to its fragment id (collisions dropped).

    A title shared by two or more corpus fragments is omitted, so a wikilink to
    it resolves to nothing rather than silently linking the wrong fragment.
    """
    title_counts = Counter(fragment.title for fragment, _ in corpus)
    return {
        fragment.title: fragment.id
        for fragment, _ in corpus
        if title_counts[fragment.title] == 1
    }


def _build_link_graph(corpus: list[tuple[Fragment, str]]) -> dict[str, set[str]]:
    """Build an undirected adjacency map from wikilinks and parent/child edges.

    Wikilink targets are resolved to fragment ids by id or by title; unresolved
    targets are dropped. Edges are bidirectional so the walk follows backlinks.

    Duplicate-title resolution is **skip-on-collision**: when two or more corpus
    fragments share a title, that title is omitted from the ``by_title`` map, so
    a ``[[Shared Title]]`` wikilink resolves to nothing rather than silently
    linking the wrong fragment. Ids are always preferred over titles —
    :func:`_wikilink_targets` checks ``by_id`` first — so an exact-id link still
    resolves even when the fragment's title is ambiguous.
    """
    by_id = {fragment.id for fragment, _ in corpus}
    # Drop ambiguous titles (see _resolve_titles): a shared title resolves to no
    # id, since mis-linking the wrong fragment is worse than dropping the link.
    by_title = _resolve_titles(corpus)
    graph: dict[str, set[str]] = {fragment.id: set() for fragment, _ in corpus}
    for fragment, body in corpus:
        neighbours = {fragment.parent_id, *fragment.child_ids}
        neighbours |= _wikilink_targets(body, by_id, by_title)
        for other in neighbours:
            if other and other in graph and other != fragment.id:
                graph[fragment.id].add(other)
                graph[other].add(fragment.id)
    return graph


def _bounded_walk(
    seed: str,
    graph: dict[str, set[str]],
    breadth: int,
    depth: int,
) -> tuple[list[str], int]:
    """Breadth-first walk from *seed*, capped at *breadth* per level, *depth* deep.

    Returns the ordered visited ids and the deepest hop actually reached.
    """
    visited = [seed]
    seen = {seed}
    frontier = [seed]
    max_depth = 0
    for hop in range(1, depth + 1):
        candidates = sorted(
            {n for node in frontier for n in graph.get(node, set()) if n not in seen}
        )
        if not candidates:
            break
        layer = candidates[:breadth]
        seen.update(layer)
        visited.extend(layer)
        frontier = layer
        max_depth = hop
    return visited, max_depth


class GraphSpecialist:
    """Graph specialist — a bounded backlink walk over the vault's link graph."""

    name = "graph"

    def gather(self, query: str, vault: Path) -> EvidenceBundle:
        """Walk the link graph from a query-matched seed within the config bounds.

        The walk respects ``author.graph_breadth_bound`` / ``graph_depth_bound``
        and reports its reach in ``walk_stats``.
        """
        config = _load_config(vault)
        corpus = _load_corpus(vault)
        by_id = {fragment.id: fragment for fragment, _ in corpus}
        if not by_id:
            return EvidenceBundle(walk_stats=WalkStats())
        graph = _build_link_graph(corpus)
        seed = _resolve_seed(query, corpus)
        visited, max_depth = _bounded_walk(
            seed,
            graph,
            config.author.graph_breadth_bound,
            config.author.graph_depth_bound,
        )
        claims = [_fragment_claim(by_id[fid]) for fid in visited if fid in by_id]
        return EvidenceBundle(
            claims=claims,
            walk_stats=WalkStats(max_depth=max_depth, fragments_visited=len(visited)),
        )


def _aggregate_dimension(
    picks: list[_DimT],
    unclassified: _DimT | None,
) -> tuple[WeightedDimension[_DimT], ...]:
    """Aggregate per-fragment canonical *picks* into weighted dimensions.

    Counts each canonical value's occurrences across the corpus, drops the
    ``unclassified`` sentinel, normalises counts into weights in ``[0.0, 1.0]``
    (the most frequent value reaching weight 1.0), and sorts weight-descending
    with the canonical value as a stable tie-break.

    Args:
        picks: One canonical enum pick per fragment (may include the
            ``unclassified`` sentinel, which is dropped).
        unclassified: The dimension's ``UNCLASSIFIED`` sentinel, or ``None``
            for dimensions whose enum has no such member (the caller has
            already dropped the absent picks; nothing further is filtered).

    Returns:
        A weight-descending tuple of :class:`WeightedDimension` entries.
    """
    counts = Counter(pick for pick in picks if pick is not unclassified)
    if not counts:
        return ()
    top = max(counts.values())
    entries = [
        WeightedDimension(value=value, weight=count / top)
        for value, count in counts.items()
    ]
    entries.sort(key=lambda wd: (-wd.weight, wd.value.value))
    return tuple(entries)


def _aggregate_optional(
    picks: list[_DimT | None],
) -> tuple[WeightedDimension[_DimT], ...]:
    """Aggregate per-fragment *picks* whose unclassified sentinel is ``None``.

    Sibling of :func:`_aggregate_dimension` for dimensions (e.g. voice register)
    whose enum has no ``UNCLASSIFIED`` member and signals an absent
    classification with ``None``. ``None`` picks are dropped, then the present
    values are aggregated with a ``None`` sentinel (which matches nothing
    real), so the weighting/sorting stay identical to the other axes.

    Args:
        picks: One canonical enum pick per fragment, or ``None`` when the
            fragment carried no classification for this dimension.

    Returns:
        A weight-descending tuple of :class:`WeightedDimension` entries.
    """
    present = [pick for pick in picks if pick is not None]
    return _aggregate_dimension(present, None)


class _FragmentSignal:
    """The canonical ontology signal a single corpus fragment carries."""

    __slots__ = ("confidence", "dosage", "frequency", "mode", "phase", "voice_register")

    def __init__(self, fragment: Fragment, body: str) -> None:
        """Classify *fragment* deterministically, keeping any frontmatter dosage.

        The rule classifier supplies frequency, phase, mode, voice register,
        and confidence from ``title + body`` keyword signal; the rule
        classifier does not detect dosage, so dosage is read from the
        fragment's existing frontmatter (``wavelength.dosage``) instead.
        ``voice_register`` and ``confidence`` are ``None`` when no keyword
        fired (neither enum has an ``UNCLASSIFIED`` member).

        Args:
            fragment: The corpus fragment to scan.
            body: The fragment's markdown body.
        """
        classified = RuleClassifier().classify(
            fragment, content=body, title=fragment.title
        )
        self.frequency: Frequency = Frequency(classified.frequency.primary)
        self.phase: Phase = Phase(classified.wavelength.phase)
        self.mode: Mode = Mode(classified.wavelength.mode)
        self.dosage: Dosage = Dosage(fragment.wavelength.dosage)
        register = classified.voice.voice_register
        # ``use_enum_values=True`` on ``VoiceClassification`` surfaces the
        # register as its plain ``str`` value (or ``None``); rewrap it into the
        # canonical enum so aggregation sorts on ``.value`` like the other axes.
        self.voice_register: VoiceRegister | None = (
            VoiceRegister(register) if register is not None else None
        )
        # Confidence is likewise surfaced as a plain ``str`` (or ``None``);
        # rewrap it so the confidence-paradox detector can compare enum members.
        confidence = classified.voice.confidence
        self.confidence: Confidence | None = (
            Confidence(confidence) if confidence is not None else None
        )


def _detect_dosage_paradoxes(
    signals: list[tuple[str, _FragmentSignal]],
) -> list[OntologyParadox]:
    """Surface same-frequency medicine-vs-toxic contradictions across fragments.

    Args:
        signals: ``(fragment_id, signal)`` pairs for the classified corpus.

    Returns:
        One :class:`OntologyParadox` per frequency held as both medicine and
        toxic; the contradiction is named, never resolved.
    """
    by_freq: dict[Frequency, dict[Dosage, str]] = {}
    for fid, sig in signals:
        if sig.frequency is Frequency.UNCLASSIFIED or sig.dosage not in (
            Dosage.MEDICINE,
            Dosage.TOXIC,
        ):
            continue
        by_freq.setdefault(sig.frequency, {}).setdefault(sig.dosage, fid)
    paradoxes: list[OntologyParadox] = []
    for freq, seen in sorted(by_freq.items(), key=lambda item: item[0].value):
        if Dosage.MEDICINE in seen and Dosage.TOXIC in seen:
            ids = (seen[Dosage.MEDICINE], seen[Dosage.TOXIC])
            paradoxes.append(
                OntologyParadox(
                    kind="dosage",
                    fragment_ids=ids,
                    description=(
                        f"{freq.value} is held as medicine in one fragment "
                        "and as toxic in another."
                    ),
                )
            )
    return paradoxes


def _pair_sort_key(pair: frozenset[str]) -> list[str]:
    """Deterministic sort key for an opposite-pair ``frozenset``.

    Frozensets have no inherent order, so paradox detection iterates the
    opposite pairs in a stable order keyed on each pair's sorted member list.
    A named key spells out that intent (the equivalent bare ``key=sorted``
    reads as a puzzle at the call site).

    Args:
        pair: One opposite pair (e.g. ``{"rising", "diminishing"}``).

    Returns:
        The pair's members sorted into a list, used purely as a sort key.
    """
    return sorted(pair)


def _detect_phase_paradoxes(
    signals: list[tuple[str, _FragmentSignal]],
) -> list[OntologyParadox]:
    """Surface opposite-phase contradictions across fragments.

    Reuses the canonical opposite-phase pairs from
    :data:`creek.generate.paradox.OPPOSITE_PHASE_PAIRS`.

    Args:
        signals: ``(fragment_id, signal)`` pairs for the classified corpus.

    Returns:
        One :class:`OntologyParadox` per opposite-phase pair present in the
        corpus; the contradiction is named, never resolved.
    """
    first_for_phase: dict[Phase, str] = {}
    for fid, sig in signals:
        if sig.phase is not Phase.UNCLASSIFIED:
            first_for_phase.setdefault(sig.phase, fid)
    paradoxes: list[OntologyParadox] = []
    for pair in sorted(OPPOSITE_PHASE_PAIRS, key=_pair_sort_key):
        phases = [Phase(value) for value in pair]
        if all(phase in first_for_phase for phase in phases):
            lo, hi = sorted(phases, key=lambda p: p.value)
            paradoxes.append(
                OntologyParadox(
                    kind="phase",
                    fragment_ids=(first_for_phase[lo], first_for_phase[hi]),
                    description=(
                        f"The topic sits on opposite phases — {lo.value} in one "
                        f"fragment and {hi.value} in another."
                    ),
                )
            )
    return paradoxes


def _detect_confidence_paradoxes(
    signals: list[tuple[str, _FragmentSignal]],
) -> list[OntologyParadox]:
    """Surface opposite-confidence contradictions across fragments.

    Reuses the canonical opposite-confidence pairs from
    :data:`creek.generate.paradox.OPPOSITE_CONFIDENCE_PAIRS` — the same pairs
    the generate-side paradox engine treats as opposites (e.g. musing vs
    conviction), so the desk and the vault agree on what a confidence
    contradiction is.

    Args:
        signals: ``(fragment_id, signal)`` pairs for the classified corpus.

    Returns:
        One :class:`OntologyParadox` per opposite-confidence pair present in the
        corpus; the contradiction is named, never resolved.
    """
    first_for_confidence: dict[Confidence, str] = {}
    for fid, sig in signals:
        if sig.confidence is not None:
            first_for_confidence.setdefault(sig.confidence, fid)
    paradoxes: list[OntologyParadox] = []
    for pair in sorted(OPPOSITE_CONFIDENCE_PAIRS, key=_pair_sort_key):
        levels = [Confidence(value) for value in pair]
        if all(level in first_for_confidence for level in levels):
            lo, hi = sorted(levels, key=lambda c: c.value)
            paradoxes.append(
                OntologyParadox(
                    kind="confidence",
                    fragment_ids=(first_for_confidence[lo], first_for_confidence[hi]),
                    description=(
                        f"The topic is held at opposite confidence levels — "
                        f"{lo.value} in one fragment and {hi.value} in another."
                    ),
                )
            )
    return paradoxes


def _ontology_claim(
    analysis: OntologyAnalysis,
    fragment_ids: list[str],
) -> EvidenceClaim:
    """Render a grounded, single-sentence summary claim from *analysis*.

    The claim always traces to every scanned fragment so the desk's
    fragment-grounding invariant holds even on an all-unclassified corpus.

    Args:
        analysis: The aggregated ontological analysis.
        fragment_ids: Every scanned fragment id.

    Returns:
        One :class:`EvidenceClaim` summarising the dominant signal.
    """
    count = len(fragment_ids)
    if analysis.frequencies:
        freq = analysis.frequencies[0].value.value
        phase = analysis.phases[0].value.value if analysis.phases else "no clear phase"
        summary = (
            f"Ontological scan of {count} fragments: dominant frequency {freq}, "
            f"phase {phase}."
        )
    else:
        summary = (
            f"Ontological scan of {count} fragments; no dominant frequency detected."
        )
    return EvidenceClaim(claim=summary, source_fragments=fragment_ids.copy())


_CONFIDENCE_SENTINELS: tuple[tuple[str, object], ...] = (
    ("frequency", Frequency.UNCLASSIFIED),
    ("phase", Phase.UNCLASSIFIED),
    ("mode", Mode.UNCLASSIFIED),
    ("dosage", Dosage.UNCLASSIFIED),
)
"""The sentinel-bearing axes whose classification rate feeds overall confidence."""


def _overall_confidence(signals: list[tuple[str, _FragmentSignal]]) -> float:
    """Aggregate corpus-wide classification coverage into a 0.0-1.0 confidence.

    The score is the mean, across fragments, of the fraction of
    sentinel-bearing axes (frequency / phase / mode / dosage) that resolved to
    a non-``UNCLASSIFIED`` value. A fully-classified corpus scores 1.0; an
    all-unclassified corpus scores 0.0; partial classification lands in
    between — a real signal the prior ``1.0 if signals else 0.0`` placeholder
    could not express. Voice register is excluded: it has no ``UNCLASSIFIED``
    sentinel, so "did it classify" is not well-defined for that axis.

    Args:
        signals: ``(fragment_id, signal)`` pairs for the classified corpus.

    Returns:
        The mean axis-coverage fraction, rounded to four decimal places, or
        ``0.0`` for an empty corpus.
    """
    if not signals:
        return 0.0
    axis_count = len(_CONFIDENCE_SENTINELS)
    total = 0.0
    for _fid, sig in signals:
        classified = sum(
            1
            for axis, sentinel in _CONFIDENCE_SENTINELS
            if getattr(sig, axis) is not sentinel
        )
        total += classified / axis_count
    return round(total / len(signals), 4)


def _build_analysis(
    signals: list[tuple[str, _FragmentSignal]],
) -> OntologyAnalysis:
    """Aggregate classified *signals* into a canonical :class:`OntologyAnalysis`.

    Each axis is aggregated independently: frequency / phase / mode / dosage
    drop their ``UNCLASSIFIED`` sentinel, while voice register (no such
    sentinel) drops the ``None`` picks via :func:`_aggregate_optional`. Dosage,
    phase, and confidence contradictions are surfaced — never resolved — and
    ``overall_confidence`` reflects the corpus-wide classification coverage
    (see :func:`_overall_confidence`).

    Args:
        signals: ``(fragment_id, signal)`` pairs for the classified corpus.

    Returns:
        The aggregated analysis with confidence scaled to axis coverage.
    """
    return OntologyAnalysis(
        frequencies=_aggregate_dimension(
            [sig.frequency for _id, sig in signals], Frequency.UNCLASSIFIED
        ),
        phases=_aggregate_dimension(
            [sig.phase for _id, sig in signals], Phase.UNCLASSIFIED
        ),
        modes=_aggregate_dimension(
            [sig.mode for _id, sig in signals], Mode.UNCLASSIFIED
        ),
        dosages=_aggregate_dimension(
            [sig.dosage for _id, sig in signals], Dosage.UNCLASSIFIED
        ),
        voice_registers=_aggregate_optional(
            [sig.voice_register for _id, sig in signals]
        ),
        paradoxes=tuple(
            _detect_dosage_paradoxes(signals)
            + _detect_phase_paradoxes(signals)
            + _detect_confidence_paradoxes(signals)
        ),
        overall_confidence=_overall_confidence(signals),
    )


class OntologySpecialist:
    """Ontology specialist — deterministic APTITUDE analytics over the corpus.

    Classifies every corpus fragment with the deterministic
    :class:`~creek.classify.rules.RuleClassifier` (no LLM, no embeddings),
    aggregates canonical frequencies / phases / modes / dosages / voice
    registers into weighted dimensions, and surfaces — never resolves — the
    dosage and phase contradictions it finds (Ontology §10.2; INC-019 canonical
    taxonomy only).
    """

    name = "ontology"

    def gather(self, query: str, vault: Path) -> EvidenceBundle:
        """Return canonical ontological analysis grounded in corpus fragments.

        Degrades to an empty bundle when the corpus is empty so the desk never
        crashes on a thin vault. When the corpus is non-empty it always emits
        at least one claim, traced to the scanned fragments.

        Args:
            query: The user query (unused — analysis is corpus-wide).
            vault: The vault to scan.

        Returns:
            An :class:`EvidenceBundle` carrying the analysis and a grounded
            summary claim, or an empty bundle for an empty corpus.
        """
        corpus = _load_corpus(vault)
        if not corpus:
            return EvidenceBundle()
        signals = [
            (fragment.id, _FragmentSignal(fragment, body)) for fragment, body in corpus
        ]
        analysis = _build_analysis(signals)
        fragment_ids = [fid for fid, _sig in signals]
        return EvidenceBundle(
            claims=[_ontology_claim(analysis, fragment_ids)],
            ontology=analysis,
        )


def default_specialists() -> list[Specialist]:
    """Return the ordered default specialist roster (graph, retrieval, ontology)."""
    return [GraphSpecialist(), RetrievalSpecialist(), OntologySpecialist()]
