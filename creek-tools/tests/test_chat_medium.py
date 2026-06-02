"""Tests for the chat medium + the medium-contract conformance harness (#457).

The chat contract is the second concrete medium — proving the contract
abstraction generalizes beyond ``research``. A reusable
``assert_contract_conformant`` validates that *any* medium contract is
well-formed (structure, weight-sum sanity, required rubric keys).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from creek.author import (
    CHAT_MAX_CHARS,
    ContractConformanceError,
    assert_contract_conformant,
    load_medium_contract,
    run_author,
)
from creek.models import MediumContract

if TYPE_CHECKING:
    from pathlib import Path


def test_chat_contract_loads(tmp_path: Path) -> None:
    """The chat contract loads and weights voice at least as high as graph."""
    contract = load_medium_contract("chat", tmp_path)

    assert contract.medium == "chat"
    assert contract.structure
    assert contract.specialist_weights["voice"] >= contract.specialist_weights["graph"]


def test_research_contract_is_conformant(tmp_path: Path) -> None:
    """The shipped research contract passes the conformance harness."""
    assert_contract_conformant(load_medium_contract("research", tmp_path))


def test_chat_contract_is_conformant(tmp_path: Path) -> None:
    """The shipped chat contract passes the conformance harness."""
    assert_contract_conformant(load_medium_contract("chat", tmp_path))


def test_harness_rejects_empty_structure() -> None:
    """A contract with no structure is non-conformant."""
    bad = MediumContract(
        medium="bad",
        structure=[],
        specialist_weights={"voice": 1.0},
        reflection_rubric={
            "ontological_accuracy": 0.34,
            "citation_completeness": 0.33,
            "voice_fidelity": 0.33,
        },
    )
    with pytest.raises(ContractConformanceError, match="structure"):
        assert_contract_conformant(bad)


def test_harness_rejects_bad_weight_sum() -> None:
    """Specialist weights that do not sum to ~1.0 are non-conformant."""
    bad = MediumContract(
        medium="bad",
        structure=["reply"],
        specialist_weights={"voice": 0.2, "graph": 0.2},
        reflection_rubric={
            "ontological_accuracy": 0.34,
            "citation_completeness": 0.33,
            "voice_fidelity": 0.33,
        },
    )
    with pytest.raises(ContractConformanceError, match="specialist_weights"):
        assert_contract_conformant(bad)


def test_harness_rejects_out_of_range_weight() -> None:
    """A weight outside [0, 1] is non-conformant even when the sum is ~1.0."""
    bad = MediumContract(
        medium="bad",
        structure=["reply"],
        specialist_weights={"voice": 1.5, "graph": -0.5},  # sums to 1.0
        reflection_rubric={
            "ontological_accuracy": 0.34,
            "citation_completeness": 0.33,
            "voice_fidelity": 0.33,
        },
    )
    with pytest.raises(ContractConformanceError, match="outside"):
        assert_contract_conformant(bad)


def test_harness_rejects_missing_rubric_keys() -> None:
    """A rubric missing a required dimension is non-conformant."""
    bad = MediumContract(
        medium="bad",
        structure=["reply"],
        specialist_weights={"voice": 1.0},
        reflection_rubric={"voice_fidelity": 1.0},
    )
    with pytest.raises(ContractConformanceError, match="rubric"):
        assert_contract_conformant(bad)


def test_harness_rejects_empty_specialist_weights() -> None:
    """An empty specialist-weight map is non-conformant."""
    bad = MediumContract(
        medium="bad",
        structure=["reply"],
        specialist_weights={},
        reflection_rubric={
            "ontological_accuracy": 0.34,
            "citation_completeness": 0.33,
            "voice_fidelity": 0.33,
        },
    )
    with pytest.raises(ContractConformanceError, match="specialist_weights is empty"):
        assert_contract_conformant(bad)


def test_rendered_text_is_serialized(tmp_path: Path) -> None:
    """``rendered_text`` is a computed field present in ``model_dump`` output."""
    draft = run_author(medium="chat", query="hey", vault=tmp_path)

    dumped = draft.model_dump()
    assert dumped["rendered_text"] == draft.body


def test_chat_produces_short_reply(tmp_path: Path) -> None:
    """``run_author(medium='chat')`` returns a short voiced reply."""
    reply = run_author(
        medium="chat",
        query="hey crawdad, quick gut-check on F4?",
        vault=tmp_path,
    )

    assert reply.medium == "chat"
    assert reply.rendered_text == reply.body
    assert 0 < len(reply.rendered_text) < CHAT_MAX_CHARS


def test_research_still_runs(tmp_path: Path) -> None:
    """The research medium is unaffected by adding chat (tracer invariant)."""
    draft = run_author(medium="research", query="What is F6?", vault=tmp_path)

    assert draft.medium == "research"
    assert draft.verdict in {"PASS", "REVISE", "ESCALATE"}
