"""Tests for the research-piece medium contract (FEAT-041 #465).

``research-piece`` is externally-facing analytical writing with heavier
citation discipline than ``essay``: it may draw on ``11-Other-Authors/`` source
material *with explicit attribution*. Its contract weights Retrieval + Ontology
and a reflection rubric that prioritises citation completeness. These tests
reuse the #457 conformance harness, mirror the essay/research medium tests, and
pin the key Definition-of-Done invariant: a run over a vault with an
``11-Other-Authors/<slug>/`` work surfaces a claim attributed to that author.
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


def test_research_piece_contract_loads_and_conforms(tmp_path: Path) -> None:
    """The research-piece contract loads and passes the conformance harness."""
    contract = load_medium_contract("research-piece", tmp_path)

    assert_contract_conformant(contract)
    assert contract.medium == "research-piece"
    assert contract.structure  # non-empty ordered sections


def test_research_piece_citation_is_strictest(tmp_path: Path) -> None:
    """research-piece cites every claim — stricter than essay's ``light``."""
    contract = load_medium_contract("research-piece", tmp_path)
    essay = load_medium_contract("essay", tmp_path)

    assert contract.citation_norm == "every-claim"
    assert essay.citation_norm == "light"


def test_research_piece_rubric_prioritises_citation(tmp_path: Path) -> None:
    """The rubric weights citation completeness highest (SPEC §8)."""
    rubric = load_medium_contract("research-piece", tmp_path).reflection_rubric

    assert rubric["citation_completeness"] == max(rubric.values())
    assert rubric["citation_completeness"] >= rubric["voice_fidelity"]


def test_research_piece_weights_retrieval_and_ontology(tmp_path: Path) -> None:
    """research-piece weights Retrieval/Ontology at least as high as Voice."""
    weights = load_medium_contract("research-piece", tmp_path).specialist_weights

    assert weights["retrieval"] >= weights["voice"]
    assert weights["ontology"] >= weights["voice"]


def test_require_supported_medium_accepts_research_piece() -> None:
    """``require_supported_medium`` accepts ``research-piece`` (it is wired)."""
    assert "research-piece" in SUPPORTED_MEDIUMS
    assert require_supported_medium("research-piece") == "research-piece"


def test_run_author_research_piece_returns_draft(tmp_path: Path) -> None:
    """``run_author(medium='research-piece')`` returns a draft (no ValueError)."""
    draft = run_author(
        medium="research-piece", query="leverage and judgement", vault=tmp_path
    )

    assert draft.medium == "research-piece"
    assert draft.body.strip()
    assert draft.verdict in {"PASS", "REVISE", "ESCALATE"}


def test_research_piece_surfaces_attributed_other_author_claim(
    tmp_path: Path,
) -> None:
    """A run over an ``11-Other-Authors/`` work surfaces an attributed claim.

    This is the key Definition-of-Done invariant: research-piece may draw on
    borrowed source material, and the borrowed claim travels with its
    ``author_slug`` so downstream citation attributes it correctly.
    """
    _write(
        tmp_path,
        "11-Other-Authors/naval-ravikant",
        "frag-naval",
        "On leverage",
        body="Leverage is the force multiplier of judgement.",
        author="other",
        author_slug="naval-ravikant",
        platform="markdown",
    )

    conductor = build_default_conductor(
        max_rounds=1, contract=load_medium_contract("research-piece", tmp_path)
    )
    evidence = conductor.gather_evidence("leverage", tmp_path)

    attributed = [c for c in evidence.claims if c.author_slug == "naval-ravikant"]
    assert attributed, "expected a claim attributed to naval-ravikant"
