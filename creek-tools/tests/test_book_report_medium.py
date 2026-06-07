"""Tests for the book-report medium contract (FEAT-041 #469).

``book-report`` synthesizes a specific ``11-Other-Authors/<author>/<work>`` work
against the owner's existing threads/eddies — "how does this book resonate with
what I already believe?" — attributing the work throughout. Its contract is
attribution-heavy (every claim cited, the rubric weighting attribution unusually
high) and weights Graph (thread/eddy linkage) + Retrieval (the work) +
Ontology. These tests reuse the #457 conformance harness, mirror
``test_research_piece_medium.py``, and pin the Definition-of-Done invariant: a
run over a vault carrying an ``11-Other-Authors/`` work *and* the owner's
thread/eddy fragments the work links to surfaces both an attributed claim and
the owner's fragments.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from creek.author import (
    SUPPORTED_MEDIUMS,
    assert_contract_conformant,
    load_medium_contract,
    require_supported_medium,
    run_author,
)
from creek.author.conductor import build_default_conductor
from tests.helpers import write_raw_fragment_file as _write

if TYPE_CHECKING:
    from pathlib import Path


def test_book_report_contract_loads_and_conforms(tmp_path: Path) -> None:
    """The book-report contract loads and passes the conformance harness."""
    contract = load_medium_contract("book-report", tmp_path)

    assert_contract_conformant(contract)
    assert contract.medium == "book-report"
    assert contract.structure  # non-empty ordered sections


def test_book_report_citation_is_strict(tmp_path: Path) -> None:
    """book-report cites every claim — stricter than essay's ``light``."""
    contract = load_medium_contract("book-report", tmp_path)
    essay = load_medium_contract("essay", tmp_path)

    assert contract.citation_norm == "every-claim"
    assert essay.citation_norm == "light"


def test_book_report_rubric_weights_attribution_heavily(tmp_path: Path) -> None:
    """The rubric weights attribution unusually high — heavier than essay (SPEC §8)."""
    rubric = load_medium_contract("book-report", tmp_path).reflection_rubric
    essay = load_medium_contract("essay", tmp_path).reflection_rubric

    assert rubric["attribution_correctness"] > essay["attribution_correctness"]
    assert rubric["citation_completeness"] >= rubric["voice_fidelity"]


def test_book_report_weights_graph_retrieval_ontology_over_voice(
    tmp_path: Path,
) -> None:
    """book-report favours Graph/Retrieval/Ontology over Voice (thread/eddy linkage)."""
    weights = load_medium_contract("book-report", tmp_path).specialist_weights

    assert weights["graph"] >= weights["voice"]
    assert weights["retrieval"] >= weights["voice"]
    assert weights["ontology"] >= weights["voice"]


def test_book_report_specialist_weights_sum_to_one(tmp_path: Path) -> None:
    """The specialist weights sum to ~1.0 (conformance invariant, SPEC §5)."""
    weights = load_medium_contract("book-report", tmp_path).specialist_weights

    assert abs(sum(weights.values()) - 1.0) < 0.01


def test_require_supported_medium_accepts_book_report() -> None:
    """``require_supported_medium`` accepts ``book-report`` (it is wired)."""
    assert "book-report" in SUPPORTED_MEDIUMS
    assert require_supported_medium("book-report") == "book-report"


def test_run_author_book_report_returns_draft(tmp_path: Path) -> None:
    """``run_author(medium='book-report')`` returns a draft (no ValueError)."""
    draft = run_author(
        medium="book-report",
        query="Report on the work, relating it to my threads and eddies.",
        vault=tmp_path,
    )

    assert draft.medium == "book-report"
    assert draft.body.strip()
    assert draft.verdict in {"PASS", "REVISE", "ESCALATE"}


def test_book_report_resonance_attributes_work_and_references_owner_threads(
    tmp_path: Path,
) -> None:
    """A book-report run attributes the work AND references the owner's threads.

    The Definition-of-Done invariant: seed (a) an ``11-Other-Authors/<author>/``
    work and (b) the owner's thread fragment the work wikilinks to, then assert a
    claim attributes the work (``author_slug``) AND the owner thread fragment id
    appears in the evidence's source fragments. Concrete id assertions, not
    vacuous truthiness.
    """
    _write(
        tmp_path,
        "01-Fragments/Threads",
        "frag-owner-leverage",
        "leverage and judgement",
        body="Leverage is how I already think about compounding judgement.",
        author="self",
    )
    _write(
        tmp_path,
        "11-Other-Authors/naval-ravikant",
        "frag-naval-leverage",
        "leverage report",
        body="Leverage multiplies judgement. [[frag-owner-leverage]]",
        author="other",
        author_slug="naval-ravikant",
        platform="markdown",
    )

    conductor = build_default_conductor(
        max_rounds=1, contract=load_medium_contract("book-report", tmp_path)
    )
    effective_query = "leverage report relating it to my threads and eddies"
    evidence = conductor.gather_evidence(effective_query, tmp_path)

    attributed = [c for c in evidence.claims if c.author_slug == "naval-ravikant"]
    assert attributed, "expected a claim attributed to naval-ravikant"
    assert any("frag-naval-leverage" in c.source_fragments for c in attributed), (
        "the attributed claim must cite the other-author work fragment"
    )
    referenced = evidence.all_source_fragments()
    assert "frag-owner-leverage" in referenced, (
        "the owner's thread fragment must surface via thread/eddy linkage"
    )
