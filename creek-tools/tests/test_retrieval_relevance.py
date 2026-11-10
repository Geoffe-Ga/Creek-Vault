"""Retrieval relevance when fragment titles do not distinguish fragments.

A fragment's embedding is computed from its **title** alone
(:func:`creek.link.embeddings.fragment_embedding_text`). A real vault has
thousands of fragments sharing a generic title (``messages``), which all embed
to one vector, tie on cosine similarity, and fall back to the fragment-id
tie-break. The desk then cited the lowest-id ``messages`` fragments for every
topic. These tests pin the fix: a lexical (BM25) score over title + body is
fused with the embedding ranking, and the graph walk does not invent a seed
when no title shares a content word with the query.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from creek.author.agents import (
    GraphSpecialist,
    RetrievalSpecialist,
    _load_corpus,
    _resolve_seed,
)
from creek.author.lexical import bm25_scores, fuse_rankings, tied_ranks
from tests.helpers import write_raw_fragment_file as _write

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture(autouse=True)
def _isolate_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """Clear any leaked ``CREEK_CONFIG`` so tests use vault defaults."""
    monkeypatch.delenv("CREEK_CONFIG", raising=False)


# --- bm25_scores ---------------------------------------------------------------


def test_bm25_scores_only_documents_sharing_a_content_word() -> None:
    """A document with no query content word is absent; a matching one scores > 0."""
    scores = bm25_scores(
        "linter bypass shortcuts",
        [("a", "boat fish tunic"), ("b", "the linter bypass again")],
    )

    assert set(scores) == {"b"}
    assert scores["b"] > 0.0


def test_bm25_ignores_stopwords_in_the_query() -> None:
    """Stopwords never match, so a stopword-only overlap scores nothing."""
    assert bm25_scores("what is the", [("a", "what is the point")]) == {}


def test_bm25_rewards_more_matched_terms() -> None:
    """Matching two rare query terms outranks matching one."""
    scores = bm25_scores(
        "agency choice",
        [("one", "agency matters"), ("two", "agency and choice matter"), ("x", "z")],
    )

    assert scores["two"] > scores["one"]


def test_bm25_weights_rare_terms_above_common_ones() -> None:
    """A term in few documents is worth more than one in most of them."""
    docs = [(f"d{i}", "today today") for i in range(8)]
    docs += [("rare", "wavelength"), ("common", "today")]
    scores = bm25_scores("wavelength today", docs)

    assert scores["rare"] > scores["common"]


def test_bm25_empty_inputs_score_nothing() -> None:
    """No documents, or a query with no content words, yields no scores."""
    assert bm25_scores("agency", []) == {}
    assert bm25_scores("", [("a", "agency")]) == {}


# --- tied_ranks / fuse_rankings -------------------------------------------------


def test_tied_ranks_share_a_rank_for_equal_scores() -> None:
    """Equal scores share one rank; the next distinct score takes the next rank."""
    assert tied_ranks({"a": 0.9, "b": 0.5, "c": 0.9, "d": 0.1}) == {
        "a": 1,
        "c": 1,
        "b": 2,
        "d": 3,
    }


def test_fusion_lets_the_lexical_score_break_an_embedding_tie() -> None:
    """When every embedding score ties, the lexical match decides the order."""
    embedding = {"frag-a": 0.4, "frag-b": 0.4, "frag-z": 0.4}

    order = fuse_rankings(embedding, {"frag-z": 3.0})

    assert order[0] == "frag-z"
    assert order[1:] == ["frag-a", "frag-b"]


def test_fusion_keeps_the_embedding_order_without_lexical_evidence() -> None:
    """With no lexical scores the fused order is the embedding order."""
    order = fuse_rankings({"a": 0.1, "b": 0.9, "c": 0.5}, {})

    assert order == ["b", "c", "a"]


def test_fusion_combines_both_signals() -> None:
    """A fragment strong on both signals beats one strong on only one."""
    embedding = {"emb-only": 0.9, "both": 0.8, "other": 0.5, "lex-only": 0.1}
    lexical = {"lex-only": 3.0, "both": 2.0}

    assert fuse_rankings(embedding, lexical)[0] == "both"


def test_fusion_ranks_only_embedding_scored_fragments() -> None:
    """A lexical-only id (dropped by the live-embed budget) is never surfaced."""
    assert fuse_rankings({"a": 0.5}, {"a": 1.0, "dropped": 9.0}) == ["a"]


# --- RetrievalSpecialist on a vault of identical titles ------------------------


def _same_title_vault(vault: Path) -> None:
    """Seven ``messages`` fragments, only the highest id about the topic."""
    for i in range(6):
        _write(
            vault,
            "01-Fragments/Messages",
            f"frag-{i}",
            "messages",
            body=f"Boat fish tunic shoes number {i}.",
        )
    _write(
        vault,
        "01-Fragments/Messages",
        "frag-z",
        "messages",
        body="Codex keeps adding noqa to bypass the linter instead of fixing it.",
    )


def test_retrieval_surfaces_the_on_topic_fragment_among_identical_titles(
    tmp_path: Path,
) -> None:
    """The body that matches the query is retrieved first, not the lowest ids."""
    _same_title_vault(tmp_path)

    bundle = RetrievalSpecialist().gather(
        "Why does my agent add noqa to bypass the linter?", tmp_path
    )

    ids = [claim.source_fragments[0] for claim in bundle.claims]
    assert ids[0] == "frag-z"
    assert len(ids) == 5


def test_retrieval_order_changes_with_the_query(tmp_path: Path) -> None:
    """Different topics retrieve different fragments from the same vault."""
    _same_title_vault(tmp_path)

    linter = RetrievalSpecialist().gather("noqa linter", tmp_path)
    tunic = RetrievalSpecialist().gather("tunic shoes", tmp_path)

    assert linter.claims[0].source_fragments == ["frag-z"]
    assert tunic.claims[0].source_fragments != ["frag-z"]


# --- Graph seed ----------------------------------------------------------------


def test_resolve_seed_returns_nothing_without_a_shared_content_word(
    tmp_path: Path,
) -> None:
    """No title shares a non-stopword with the query, so there is no seed."""
    _same_title_vault(tmp_path)

    corpus = _load_corpus(tmp_path, None)

    assert _resolve_seed("agency and free will", corpus) == ""


def test_resolve_seed_ignores_stopword_overlap(tmp_path: Path) -> None:
    """Sharing only a stopword ("what") with a title is not a match."""
    _write(tmp_path, "01-Fragments/Notes", "frag-a", "What I learned")

    corpus = _load_corpus(tmp_path, None)

    assert _resolve_seed("What is agency?", corpus) == ""


def test_resolve_seed_still_picks_the_best_title_match(tmp_path: Path) -> None:
    """A real content-word overlap still selects the best-matching title."""
    _write(tmp_path, "01-Fragments/Notes", "frag-a", "Agency and choice")
    _write(tmp_path, "01-Fragments/Notes", "frag-b", "Fishing trip")

    corpus = _load_corpus(tmp_path, None)

    assert _resolve_seed("what agency is", corpus) == "frag-a"


def test_graph_without_a_seed_returns_no_claims(tmp_path: Path) -> None:
    """The graph walk contributes nothing rather than an arbitrary fragment."""
    _same_title_vault(tmp_path)

    bundle = GraphSpecialist().gather("agency and free will", tmp_path)

    assert bundle.claims == []
    assert bundle.walk_stats is not None
    assert bundle.walk_stats.fragments_visited == 0


def test_fusion_ranks_lexical_scores_only_among_embedding_scored_fragments() -> None:
    """Dropped fragments do not dilute an admitted fragment's lexical rank.

    If the thousand budget-dropped fragments took lexical ranks 1-1000, the one
    admitted lexical match would rank 1001 and its bonus would vanish, leaving
    the embedding-only leader in front.
    """
    embedding = {"lead": 0.9, "f2": 0.8, "f3": 0.7, "f4": 0.6, "match": 0.1}
    lexical = {"match": 1.0} | {f"dropped-{i}": 2.0 + i for i in range(1000)}

    assert fuse_rankings(embedding, lexical)[0] == "match"
