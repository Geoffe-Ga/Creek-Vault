"""Specialist agents for the Creek Writing Desk (FEAT-041).

The Graph and Retrieval specialists are real (#463): Graph walks a bounded
backlink graph over the vault; Retrieval ranks fragments by semantic similarity
to the query, reusing :mod:`creek.link.embeddings`. The Ontology specialist
remains a stub (its real logic is #467). Every specialist returns structured,
provenance-tracked :class:`~creek.author.models.EvidenceBundle`s — claims paired
with their ``source_fragments`` (and an ``author_slug`` for borrowed evidence).
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Protocol, runtime_checkable

import numpy as np

from creek.author.models import EvidenceBundle, EvidenceClaim, WalkStats
from creek.link.embeddings import (
    EmbeddingLinker,
    EmbeddingModelUnavailableError,
    fragment_embedding_text,
)
from creek.vault.reader import iter_vault_fragments

if TYPE_CHECKING:
    from pathlib import Path

    from creek.config import CreekConfig, EmbeddingsConfig
    from creek.models import Fragment

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


def _rank_fragments(
    query: str,
    corpus: list[tuple[Fragment, str]],
    config: EmbeddingsConfig,
) -> list[Fragment]:
    """Rank *corpus* fragments by semantic similarity to *query* (descending).

    Reuses :class:`EmbeddingLinker`. Ties break by fragment id for
    determinism. Raises :class:`EmbeddingModelUnavailableError` when the
    embedding model cannot load.
    """
    linker = EmbeddingLinker(config)
    query_vec = linker.generate_embedding(query)
    scored: list[tuple[float, str, Fragment]] = []
    for fragment, _body in corpus:
        vec = linker.generate_embedding(fragment_embedding_text(fragment))
        scored.append((_cosine(query_vec, vec), fragment.id, fragment))
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [fragment for _score, _id, fragment in scored]


class RetrievalSpecialist:
    """Semantic-retrieval specialist over raw, reference, and other-author text."""

    name = "retrieval"

    def gather(self, query: str, vault: Path) -> EvidenceBundle:
        """Return the top-``retrieval_top_k`` fragments most relevant to *query*.

        Degrades to an empty bundle when the corpus is empty or the embedding
        model is unavailable, so the desk never crashes on a thin/offline vault.
        """
        config = _load_config(vault)
        corpus = _load_corpus(vault)
        if not corpus:
            return EvidenceBundle()
        try:
            ranked = _rank_fragments(query, corpus, config.embeddings)
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


def _build_link_graph(corpus: list[tuple[Fragment, str]]) -> dict[str, set[str]]:
    """Build an undirected adjacency map from wikilinks and parent/child edges.

    Wikilink targets are resolved to fragment ids by id or by title; unresolved
    targets are dropped. Edges are bidirectional so the walk follows backlinks.
    """
    by_id = {fragment.id for fragment, _ in corpus}
    by_title = {fragment.title: fragment.id for fragment, _ in corpus}
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
    for current in range(1, depth + 1):
        candidates = sorted(
            {n for node in frontier for n in graph.get(node, set()) if n not in seen}
        )
        if not candidates:
            break
        layer = candidates[:breadth]
        seen.update(layer)
        visited.extend(layer)
        frontier = layer
        max_depth = current
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


class OntologySpecialist:
    """Stub Ontology specialist — would ground claims in the APTITUDE model (#467)."""

    name = "ontology"

    def gather(self, query: str, vault: Path) -> EvidenceBundle:
        """Return a mock ontology-derived claim (stub; real logic is #467)."""
        return EvidenceBundle(
            claims=[
                EvidenceClaim(
                    claim=f"Ontology framing for {query!r} (stub).",
                    source_fragments=["frag-ontology-0001"],
                )
            ]
        )


def default_specialists() -> list[Specialist]:
    """Return the ordered default specialist roster (graph, retrieval, ontology)."""
    return [GraphSpecialist(), RetrievalSpecialist(), OntologySpecialist()]
