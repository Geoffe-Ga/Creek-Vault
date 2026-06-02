"""Tests for the essay medium contract (FEAT-041 #461).

``essay`` is the third concrete medium — long-form, voiced Substack-style
writing. Its contract weights Voice + Retrieval and a reflection rubric that
prioritizes voice fidelity. These tests reuse the #457 conformance harness.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from creek.author import (
    assert_contract_conformant,
    load_medium_contract,
    run_author,
)

if TYPE_CHECKING:
    from pathlib import Path


def test_essay_contract_loads_and_conforms(tmp_path: Path) -> None:
    """The essay contract loads and passes the conformance harness."""
    contract = load_medium_contract("essay", tmp_path)

    assert_contract_conformant(contract)
    assert contract.medium == "essay"
    assert contract.structure  # non-empty


def test_essay_rubric_prioritises_voice_fidelity(tmp_path: Path) -> None:
    """The essay reflection rubric weights voice fidelity highest (SPEC §8)."""
    rubric = load_medium_contract("essay", tmp_path).reflection_rubric

    assert rubric["voice_fidelity"] == max(rubric.values())


def test_essay_weights_voice_and_retrieval(tmp_path: Path) -> None:
    """Essay weights Voice and Retrieval at least as high as Graph/Ontology."""
    weights = load_medium_contract("essay", tmp_path).specialist_weights

    assert weights["voice"] >= weights["graph"]
    assert weights["retrieval"] >= weights["ontology"]


def test_run_author_essay_returns_draft(tmp_path: Path) -> None:
    """``run_author(medium='essay')`` returns a voiced draft (tracer invariant)."""
    draft = run_author(medium="essay", query="leverage vs. presence", vault=tmp_path)

    assert draft.medium == "essay"
    assert draft.body.strip()
    assert draft.verdict in {"PASS", "REVISE", "ESCALATE"}
