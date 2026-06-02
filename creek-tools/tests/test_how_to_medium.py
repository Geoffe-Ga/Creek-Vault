"""Tests for the how-to medium contract (FEAT-041 #472).

``how-to`` is the agentic-coding-guide medium: structured, example-driven
technical writing that weights the **Graph + Ontology** specialists toward
Technical fragments and Praxis linkage. Its reflection rubric carries an
explicit **accuracy/runnability** dimension — the cardinal failure mode for a
guide is steps that do not actually run. These tests reuse the #457 conformance
harness, mirror ``test_book_report_medium.py``, and pin the Definition-of-Done
invariant: the structure is example-driven and the rubric weights runnability
meaningfully.
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

if TYPE_CHECKING:
    from pathlib import Path


def test_how_to_contract_loads_and_conforms(tmp_path: Path) -> None:
    """The how-to contract loads and passes the conformance harness."""
    contract = load_medium_contract("how-to", tmp_path)

    assert_contract_conformant(contract)
    assert contract.medium == "how-to"
    assert contract.structure  # non-empty ordered sections


def test_how_to_structure_is_example_driven(tmp_path: Path) -> None:
    """The structure is concrete + example-driven (example/verification/steps)."""
    structure = load_medium_contract("how-to", tmp_path).structure

    assert "example" in structure
    assert "verification" in structure
    assert "steps" in structure


def test_how_to_weights_graph_and_ontology_highest(tmp_path: Path) -> None:
    """Graph + Ontology each outweigh retrieval and voice (Technical + Praxis)."""
    weights = load_medium_contract("how-to", tmp_path).specialist_weights

    assert weights["graph"] >= weights["retrieval"]
    assert weights["graph"] >= weights["voice"]
    assert weights["ontology"] >= weights["retrieval"]
    assert weights["ontology"] >= weights["voice"]


def test_how_to_specialist_weights_sum_to_one(tmp_path: Path) -> None:
    """The specialist weights sum to ~1.0 (conformance invariant, SPEC §5)."""
    weights = load_medium_contract("how-to", tmp_path).specialist_weights

    assert abs(sum(weights.values()) - 1.0) < 0.01


def test_how_to_rubric_weights_runnability_meaningfully(tmp_path: Path) -> None:
    """The rubric carries a meaningfully-weighted runnability dimension (the DoD)."""
    rubric = load_medium_contract("how-to", tmp_path).reflection_rubric

    assert "runnability" in rubric
    # The cardinal failure mode → it must carry real weight, not a token sliver.
    assert rubric["runnability"] >= 0.25
    assert rubric["runnability"] >= rubric["voice_fidelity"]
    assert abs(sum(rubric.values()) - 1.0) < 0.01


def test_how_to_citation_norm_is_light(tmp_path: Path) -> None:
    """how-to favours runnable steps over dense citation (``light``)."""
    contract = load_medium_contract("how-to", tmp_path)

    assert contract.citation_norm == "light"


def test_require_supported_medium_accepts_how_to() -> None:
    """``require_supported_medium`` accepts ``how-to`` (it is wired)."""
    assert "how-to" in SUPPORTED_MEDIUMS
    assert require_supported_medium("how-to") == "how-to"


def test_run_author_how_to_returns_draft(tmp_path: Path) -> None:
    """``run_author(medium='how-to')`` returns a draft (no ValueError)."""
    draft = run_author(
        medium="how-to",
        query="How do I wire the classifier end to end?",
        vault=tmp_path,
    )

    assert draft.medium == "how-to"
    assert draft.body.strip()
    assert draft.verdict in {"PASS", "REVISE", "ESCALATE"}
