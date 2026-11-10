"""Lexical relevance for Writing Desk retrieval: BM25 plus tie-aware rank fusion.

Why this exists. A fragment's embedding is computed from its **title** alone
(:func:`creek.link.embeddings.fragment_embedding_text`), and a real vault holds
thousands of fragments under one generic title (``messages``). Those all embed
to one vector, tie on cosine similarity, and the id tie-break then picks the
same handful for every topic. The embedding cache is shared with ``creek link``,
so widening the embedded text would invalidate every vault's cache and push a
cold desk call into tens of thousands of live embeds. A lexical score over the
fragment's own words separates those fragments cheaply, and fusing it with the
embedding ranking keeps the embedding's signal wherever titles do differ.

Fusion is reciprocal-rank fusion over **tied** ranks: equal embedding scores
share one rank, so a 30,000-way tie contributes the same amount to every
fragment in it and the lexical rank decides among them, rather than the
fragment id. Everything here is pure, local and deterministic; nothing leaves
the process.
"""

from __future__ import annotations

import math
from collections import Counter
from typing import TYPE_CHECKING, Final

from creek.link.naming import content_words

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

#: BM25 term-frequency saturation.
BM25_K1: Final[float] = 1.2

#: BM25 document-length normalisation strength.
BM25_B: Final[float] = 0.75

#: Characters of each document scored. Bounds the per-call cost on a large
#: vault; the opening of a fragment carries its topic.
LEXICAL_MAX_CHARS: Final[int] = 4000

#: Reciprocal-rank-fusion damping constant (the conventional 60).
RRF_K: Final[int] = 60

#: Decimal places an embedding score is rounded to before ranks are tied, so
#: float noise between identical vectors cannot split a genuine tie.
_TIE_DECIMALS: Final[int] = 9


def bm25_scores(query: str, documents: Sequence[tuple[str, str]]) -> dict[str, float]:
    """Return the Okapi BM25 score of each document sharing a word with *query*.

    Words are lowercase alphabetic content words (stopwords and words under
    three letters dropped), so an overlap on "what" or "the" counts for
    nothing. Each document is truncated to :data:`LEXICAL_MAX_CHARS`.

    Args:
        query: The text to score documents against.
        documents: ``(id, text)`` pairs.

    Returns:
        Positive scores keyed by id. A document with no query content word is
        absent rather than scored zero.
    """
    terms = set(content_words(query))
    if not terms or not documents:
        return {}
    counts = [_matched_terms(doc_id, text, terms) for doc_id, text in documents]
    average = sum(length for _, _, length in counts) / len(counts) or 1.0
    idf = _idf(counts)
    scores: dict[str, float] = {}
    for doc_id, matched, length in counts:
        if matched:
            norm = BM25_K1 * (1 - BM25_B + BM25_B * length / average)
            scores[doc_id] = sum(
                idf[term] * tf * (BM25_K1 + 1) / (tf + norm)
                for term, tf in matched.items()
            )
    return scores


def _matched_terms(
    doc_id: str, text: str, terms: set[str]
) -> tuple[str, Counter[str], int]:
    """Return *text*'s counts of the query *terms* and its content-word length."""
    words = content_words(text[:LEXICAL_MAX_CHARS])
    return doc_id, Counter(word for word in words if word in terms), len(words)


def _idf(counts: Sequence[tuple[str, Counter[str], int]]) -> dict[str, float]:
    """Return the BM25 inverse document frequency of every matched term."""
    total = len(counts)
    frequency = Counter(term for _, matched, _ in counts for term in matched)
    return {
        term: math.log(1 + (total - df + 0.5) / (df + 0.5))
        for term, df in frequency.items()
    }


def tied_ranks(scores: Mapping[str, float]) -> dict[str, int]:
    """Return dense 1-based ranks, highest score first, equal scores sharing one.

    Args:
        scores: Score per id.

    Returns:
        Rank per id; the next distinct score after a tie takes the next rank.
    """
    distinct = sorted({round(value, _TIE_DECIMALS) for value in scores.values()})
    rank_of = {value: rank for rank, value in enumerate(reversed(distinct), start=1)}
    return {key: rank_of[round(value, _TIE_DECIMALS)] for key, value in scores.items()}


def fuse_rankings(
    embedding: Mapping[str, float], lexical: Mapping[str, float]
) -> list[str]:
    """Order the embedding-scored ids by reciprocal-rank fusion with *lexical*.

    Only ids in *embedding* are ranked: an id the embedding pass dropped (for
    example under a live-embed budget) stays dropped even when it scores
    lexically, so fusion can only reorder, never widen, what ranking admitted.

    Args:
        embedding: Cosine similarity per id.
        lexical: BM25 score per id; an absent id contributes no lexical term.

    Returns:
        Ids in fused order, fragment id as the final tie-break.
    """
    embedding_rank = tied_ranks(embedding)
    lexical_rank = tied_ranks({k: v for k, v in lexical.items() if k in embedding})

    def fused(doc_id: str) -> float:
        score = 1.0 / (RRF_K + embedding_rank[doc_id])
        if doc_id in lexical_rank:
            score += 1.0 / (RRF_K + lexical_rank[doc_id])
        return score

    return sorted(embedding, key=lambda doc_id: (-fused(doc_id), doc_id))
