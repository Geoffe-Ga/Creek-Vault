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
from creek.classify.privacy_filter import PrivacyTierOverride, tier_within_override
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
from creek.models import (
    Confidence,
    Dosage,
    Frequency,
    Mode,
    Phase,
    PrivacyTier,
    VoiceRegister,
)
from creek.vault.reader import CORPUS_SUBDIRS, iter_vault_fragments

if TYPE_CHECKING:
    from enum import StrEnum
    from pathlib import Path

    from creek.config import CreekConfig
    from creek.link.embeddings import CachedEmbedding
    from creek.models import Fragment

_DimT = TypeVar("_DimT", bound="StrEnum")

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

    def gather(
        self,
        query: str,
        vault: Path,
        *,
        override: PrivacyTierOverride | None = None,
    ) -> EvidenceBundle:
        """Return structured evidence for *query* drawn from *vault*.

        *override* is the privacy admission ceiling (#660): fragments above it
        are excluded from the evidence. ``None`` defaults to ``OPEN``.
        """
        ...


def _load_config(vault: Path) -> CreekConfig:
    """Load the vault's config (defaults when no file is present)."""
    from creek.config import load_vault_config

    return load_vault_config(vault)


def _load_corpus(
    vault: Path,
    override: PrivacyTierOverride | None = None,
) -> list[tuple[Fragment, str]]:
    """Return ``(fragment, body)`` records across the corpus subtrees.

    Fragments whose ``privacy_tier`` exceeds *override* are excluded (#660), so
    above-ceiling content never enters ranking, the link graph, or the evidence.
    ``override`` of ``None`` defaults to ``OPEN`` (the most restrictive).
    """
    records: list[tuple[Fragment, str]] = []
    for sub in CORPUS_SUBDIRS:
        for _path, fragment, body, _meta in iter_vault_fragments(vault / sub):
            if tier_within_override(fragment.privacy_tier, override):
                records.append((fragment, body))
    return records


def fragment_tier_map(
    vault: Path,
    override: PrivacyTierOverride | None = None,
) -> dict[str, PrivacyTier]:
    """Return a ``{fragment_id: privacy_tier}`` map for the admitted corpus (#661).

    Built from the same override-filtered corpus the specialists gather from, so
    the desk can derive a run's *content tier* (the most-sensitive tier actually
    in the evidence) and route the voice call accordingly.

    This is the **admitted** view of the corpus, and it diverges from the leak
    gate's on purpose. Here an id seen twice resolves last-wins in
    :data:`~creek.vault.reader.CORPUS_SUBDIRS` order over records that already
    passed ``tier_within_override``, because the question is "what is in the
    evidence this run may use". The HARD gate at
    :func:`creek.author.checks._resolve_cited_tiers` reads the *unfiltered*
    corpus and resolves most-restrictive-wins, because its question is "what is
    the true tier of the text this draft reproduces" — a gate that only saw the
    admitted view would be blind to exactly the content it exists to catch.
    The divergence is deliberate, and pinned by
    ``test_leak_gate_reads_the_true_tier_while_the_router_reads_the_admitted_one``
    in ``tests/test_reflection.py``, so a future "make these agree" cleanup has
    to argue with a named assertion rather than a silent assumption.

    Args:
        vault: Vault root.
        override: The privacy admission ceiling (matches the gather override).

    Returns:
        A mapping of admitted fragment id to its :class:`PrivacyTier`.
    """
    return {
        fragment.id: fragment.privacy_tier
        for fragment, _ in _load_corpus(vault, override)
    }


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


def _cached_vector(
    fragment: Fragment,
    cache: dict[str, CachedEmbedding],
) -> list[float] | None:
    """Return *fragment*'s cached embedding, or ``None`` when none is fresh.

    A cached vector is trusted only when the fragment id is in *cache* and its
    stored ``content_hash`` still equals the SHA-256 of the fragment's current
    :func:`fragment_embedding_text` — the exact freshness key
    :meth:`EmbeddingLinker.build_cache_entries` writes. A miss or a stale hash
    yields ``None``, and the caller falls back to a live
    :meth:`EmbeddingLinker.generate_embedding`, so the result is identical to
    embedding from scratch.

    **Why this returns ``None`` instead of embedding.** Deciding to spend a live
    embed is the caller's call, because only :func:`_rank_fragments` knows how
    many are left in the per-call budget. Folding the fallback in here would
    make the spend invisible to the counter.

    Args:
        fragment: The corpus fragment whose vector is wanted.
        cache: Persisted, model-filtered cache keyed by fragment id.

    Returns:
        The fragment's cached vector, or ``None`` on a miss or a stale hash.
    """
    cached = cache.get(fragment.id)
    if cached is None:
        return None
    fresh_hash = content_hash_for_text(fragment_embedding_text(fragment))
    if cached.content_hash != fresh_hash:
        return None
    return cached.vector


def _rank_fragments(
    query: str,
    corpus: list[tuple[Fragment, str]],
    linker: EmbeddingLinker,
    cache: dict[str, CachedEmbedding],
    max_live_embeds: int | None = None,
) -> list[Fragment]:
    """Rank *corpus* fragments by semantic similarity to *query* (descending).

    Embeds the query once with the reused *linker* and scores each fragment
    against it, drawing fresh cached vectors from *cache* to avoid re-embedding
    unchanged fragments (the cache is a pure optimisation — a hit yields the
    same vector a live embed would). Ties break by fragment id for determinism.
    Raises :class:`EmbeddingModelUnavailableError` when the embedding model
    cannot load.

    **The live-embed budget.** *max_live_embeds* bounds how many cache misses
    this one call may embed from scratch; ``None`` (the default) is unbounded,
    i.e. byte-for-byte today's behaviour, which is what keeps
    :func:`default_specialists` and the Writing Desk unchanged. When the budget
    runs out the remaining un-cached fragments are **dropped from the ranking**
    rather than embedded. That is a strict *narrowing*: a dropped fragment
    yields no title, no id and no body, and the budget is applied only to a
    corpus that ``_load_corpus`` has already filtered by the caller's override,
    so it can never admit a fragment the ceiling excluded.

    **Spend order is deterministic and vault-layout independent.** The corpus is
    walked in fragment-id order, not in ``CORPUS_SUBDIRS``/directory order, so
    *which* fragments spend a bounded budget does not depend on filesystem
    traversal. The unbounded path is provably unaffected by this: the final
    ``scored.sort`` keys on ``(-score, id)``, a total order, so the input
    iteration order cannot change the output when nothing is dropped.

    Args:
        query: The user query to rank fragments against.
        corpus: ``(fragment, body)`` records to score.
        linker: The reused embedding linker.
        cache: Persisted, model-filtered embedding cache keyed by fragment id.
        max_live_embeds: Maximum cache misses this call may embed live, or
            ``None`` for unbounded (the default, and today's behaviour).

    Returns:
        Fragments ordered by cosine similarity descending, id tie-break.
    """
    query_vec = linker.generate_embedding(query)
    remaining = max_live_embeds
    scored: list[tuple[float, str, Fragment]] = []
    for fragment, _body in sorted(corpus, key=lambda record: record[0].id):
        vec = _cached_vector(fragment, cache)
        if vec is None:
            if remaining is not None and remaining <= 0:
                continue
            if remaining is not None:
                remaining -= 1
            vec = linker.generate_embedding(fragment_embedding_text(fragment))
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

    **Owner-scoped lifetime is not a violation of that (#1034).** "Never a
    module global" bounds the *scope*, not the *lifetime*: an instance owned by
    one ``build_server`` / one ``create_app`` and reused for that owner's calls
    is still per-owner state that a fresh test constructs cold. What is
    forbidden is a process-global or an ``lru_cache``, either of which would
    carry one test's mocked model into the next.
    :class:`creek_mcp.tools.reflect.GroundingSession` is that owner-scoped
    holder, and it keeps exactly one specialist at a time.

    **What may and may not be shared across calls.** Only the two memo slots —
    the model handle and the parquet id→vector map — are call-independent. The
    parquet is read unfiltered and is already tier-blind, and the map is only
    ever *read* for ids the current call independently admitted, so sharing it
    changes **when** it is read, never **what** it contains. Everything
    override-derived — the corpus record list, the ranked order, the top-k
    slice, the returned bundle and the live-embed counter — is a local of
    :meth:`gather` and must stay one, because ``_load_config`` and
    ``_load_corpus`` re-decide admission on every call from *that* call's
    override. An override-derived instance attribute would be a privacy leak,
    not an optimisation, and
    ``test_a_warmed_specialist_mutates_nothing_across_calls_at_any_ceiling``
    fails on the instance key set the moment one is added.

    **Two consequences of the longer lifetime, decided rather than discovered.**

    - :meth:`_get_linker` binds ``config.embeddings`` at first use, so an
      operator editing ``embeddings.model`` under a running server is picked up
      at restart rather than on the next call. Keying by vault does not close
      this: the vault path is unchanged.
    - A re-run of ``creek link`` no longer lands on the next reflection. This is
      harmless because :func:`_cached_vector` validates ``content_hash`` against
      the fragment's current text, so a stale map entry can only cause a live
      re-embed — never a wrong vector.
    """

    name = "retrieval"

    def __init__(
        self,
        *,
        linker: EmbeddingLinker | None = None,
        max_live_embeds: int | None = None,
    ) -> None:
        """Initialise the specialist with an optional pre-built linker.

        Args:
            linker: An embedding linker to reuse; when ``None`` (the default,
                as in :func:`default_specialists`) one is created lazily on
                first :meth:`gather` and cached on the instance.
            max_live_embeds: Maximum cache misses any one :meth:`gather` may
                embed live, or ``None`` (the default) for unbounded — today's
                behaviour, which is what keeps the Writing Desk unchanged. This
                is a fixed ceiling, not a counter: the counter is a local of
                :func:`_rank_fragments`, so one call's spend never bounds the
                next one's.
        """
        self._linker = linker
        self._cache: dict[str, CachedEmbedding] | None = None
        self._cache_vault: Path | None = None
        self._max_live_embeds = max_live_embeds

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
        Staleness is harmless: ``_cached_vector`` validates each entry's
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

    def warm(self, vault: Path) -> None:
        """Fill both memo slots for *vault* now, so no :meth:`gather` has to.

        Exists for the concurrency case (#1034). ``/v1`` serves reads in anyio
        worker threads with several per-consumer slots, so two first requests
        can be inside :meth:`gather` at once; both would find ``self._linker``
        and ``self._cache`` empty and each would construct a linker and read the
        parquet. Atomic rebinding prevents corruption, not duplication. Doing
        every instance mutation **once, before the instance is shared**, is what
        makes "one construction, one parquet read, one model load" true rather
        than merely likely — and what lets
        ``test_a_warmed_specialist_mutates_nothing_across_calls_at_any_ceiling``
        assert that a warmed specialist is thereafter read-only.

        The one mutation left inside a concurrent :meth:`gather` is
        :meth:`EmbeddingLinker.load_model`'s lazy ``self._model`` assignment,
        reached on the first live embed. It is idempotent and rebinds
        atomically, so the worst case is two model loads with one winner —
        never a wrong vector. Callers wanting even that bounded should warm
        before sharing, which
        :class:`creek_mcp.tools.reflect.GroundingSession` does.

        **This does slightly more than :meth:`gather` alone would.** ``gather``
        returns early on an empty corpus *before* touching either memo, so a
        vault with no admitted corpus never reads the parquet path today; after
        ``warm`` it does. Benign — ``load_cache`` returns ``{}`` for a missing
        path — but it is a real behaviour change, named here rather than
        discovered. No admission decision is involved: ``warm`` reads the
        model-filtered map, which is tier-blind, and takes no override.

        Args:
            vault: The vault whose config, linker and embeddings cache to load.
        """
        self._get_cache(self._get_linker(_load_config(vault)), vault)

    def gather(
        self,
        query: str,
        vault: Path,
        *,
        override: PrivacyTierOverride | None = None,
    ) -> EvidenceBundle:
        """Return the top-``retrieval_top_k`` fragments most relevant to *query*.

        Degrades to an empty bundle when the corpus is empty or the embedding
        model is unavailable, so the desk never crashes on a thin/offline vault.
        The persisted embeddings cache is loaded once and used to skip
        re-embedding fragments whose text is unchanged. Fragments above
        *override* are excluded from the corpus (#660).

        ``_load_config`` and ``_load_corpus`` run on **every** call, before
        either memo is consulted, so admission is re-decided per call from
        *this* caller's override — a memo can never carry admitted content
        across calls. When the instance was built with ``max_live_embeds``,
        that ceiling bounds how many cache misses this call embeds live; the
        counter is a local of :func:`_rank_fragments`, never instance state.
        """
        config = _load_config(vault)
        corpus = _load_corpus(vault, override)
        if not corpus:
            return EvidenceBundle()
        linker = self._get_linker(config)
        cache = self._get_cache(linker, vault)
        try:
            ranked = _rank_fragments(
                query, corpus, linker, cache, self._max_live_embeds
            )
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

    def gather(
        self,
        query: str,
        vault: Path,
        *,
        override: PrivacyTierOverride | None = None,
    ) -> EvidenceBundle:
        """Walk the link graph from a query-matched seed within the config bounds.

        The walk respects ``author.graph_breadth_bound`` / ``graph_depth_bound``
        and reports its reach in ``walk_stats``. Fragments above *override* are
        excluded from the corpus before the graph is built (#660), so the walk
        never traverses or surfaces above-ceiling content.
        """
        config = _load_config(vault)
        corpus = _load_corpus(vault, override)
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
        Reading the *original* fragment for that used to be load-bearing:
        the rule pass rebuilt the whole wavelength block and blanked the
        dosage on its way past, so ``classified.wavelength.dosage`` was a
        sentinel. Since #1331 the pass merges instead, and the two agree —
        the explicit read is kept because it still states the intent, not
        because it is the only thing that works.
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

    def gather(
        self,
        query: str,
        vault: Path,
        *,
        override: PrivacyTierOverride | None = None,
    ) -> EvidenceBundle:
        """Return canonical ontological analysis grounded in corpus fragments.

        Degrades to an empty bundle when the corpus is empty so the desk never
        crashes on a thin vault. When the corpus is non-empty it always emits
        at least one claim, traced to the scanned fragments. Fragments above
        *override* are excluded from the corpus (#660).

        Args:
            query: The user query (unused — analysis is corpus-wide).
            vault: The vault to scan.
            override: The privacy admission ceiling; ``None`` defaults to OPEN.

        Returns:
            An :class:`EvidenceBundle` carrying the analysis and a grounded
            summary claim, or an empty bundle for an empty corpus.
        """
        corpus = _load_corpus(vault, override)
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
