"""Tests for the MediumContract model, loader, lint, and Conductor wiring (#459).

The ``research`` contract is the first concrete medium contract. These tests pin
its load/validation, the deployed-over-template precedence, the skill-budget
lint covering ``mediums/``, and that the Conductor exposes the contract's
reflection rubric to the (stub) reflection node.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest

from creek.author.agents import GraphSpecialist
from creek.author.conductor import Conductor
from creek.author.contracts import load_medium_contract
from creek.author.models import ReflectionResult
from creek.author.voice import VoiceAgent
from creek.lint.checks import skill_size_budget
from creek.models import MediumContract, PrivacyTier

if TYPE_CHECKING:
    from pathlib import Path

_MEDIUMS_SUBDIR = ("00-Creek-Meta", "Skills", "mediums")


def test_research_contract_loads_from_template(tmp_path: Path) -> None:
    """The research contract loads (template fallback) with the expected shape."""
    contract = load_medium_contract("research", tmp_path)

    assert isinstance(contract, MediumContract)
    assert contract.medium == "research"
    assert contract.citation_norm == "every-claim"
    assert contract.default_privacy_tier == PrivacyTier.OPEN
    assert contract.structure  # non-empty ordered sections


def test_research_weights_graph_over_voice(tmp_path: Path) -> None:
    """Research weights Graph/Ontology at least as high as Voice (SPEC §5)."""
    weights = load_medium_contract("research", tmp_path).specialist_weights

    assert weights["graph"] >= weights["voice"]
    assert weights["ontology"] >= weights["voice"]


def test_research_rubric_prioritises_accuracy_and_citation(tmp_path: Path) -> None:
    """The research rubric weights accuracy + citation above voice (SPEC §8)."""
    rubric = load_medium_contract("research", tmp_path).reflection_rubric

    assert rubric["ontological_accuracy"] >= rubric["voice_fidelity"]
    assert rubric["citation_completeness"] >= rubric["voice_fidelity"]


def test_deployed_contract_takes_precedence(tmp_path: Path) -> None:
    """A vault-deployed contract is preferred over the canonical template."""
    folder = tmp_path.joinpath(*_MEDIUMS_SUBDIR)
    folder.mkdir(parents=True)
    (folder / "research.MEDIUM.md").write_text(
        "---\nmedium: research\ncitation_norm: none\n---\n# override\n",
        encoding="utf-8",
    )

    contract = load_medium_contract("research", tmp_path)

    assert contract.citation_norm == "none"


def test_missing_contract_raises(tmp_path: Path) -> None:
    """An unknown medium with no contract raises a clear error."""
    with pytest.raises(FileNotFoundError, match="No medium contract"):
        load_medium_contract("does-not-exist", tmp_path)


def test_skill_budget_lint_flags_over_budget_contract(tmp_path: Path) -> None:
    """The skill-budget lint covers ``mediums/*.MEDIUM.md`` (FEAT-041 §5)."""
    folder = tmp_path.joinpath(*_MEDIUMS_SUBDIR)
    folder.mkdir(parents=True)
    over_budget = "word " * (skill_size_budget.SKILL_BUDGET_WORDS + 50)
    (folder / "bloated.MEDIUM.md").write_text(over_budget, encoding="utf-8")

    result = skill_size_budget.run(tmp_path)

    assert any("bloated.MEDIUM.md" in finding for finding in result.findings)


def test_skill_budget_lint_passes_within_budget_contract(tmp_path: Path) -> None:
    """A within-budget medium contract is not flagged."""
    folder = tmp_path.joinpath(*_MEDIUMS_SUBDIR)
    folder.mkdir(parents=True)
    (folder / "lean.MEDIUM.md").write_text("a few words\n", encoding="utf-8")

    result = skill_size_budget.run(tmp_path)

    assert not any("lean.MEDIUM.md" in finding for finding in result.findings)


def test_conductor_exposes_rubric_to_reflection(tmp_path: Path) -> None:
    """The Conductor passes the contract's reflection rubric to the reflection node."""
    contract = MediumContract(medium="research", reflection_rubric={"accuracy": 1.0})
    reflection = MagicMock()
    reflection.review.return_value = ReflectionResult(decision="PASS")
    conductor = Conductor(
        specialists=[GraphSpecialist()],
        voice=VoiceAgent(),
        reflection=reflection,
        max_rounds=1,
        contract=contract,
    )

    conductor.run(medium="research", query="q", vault=tmp_path)

    passed_rubric = reflection.review.call_args.args[2]
    assert passed_rubric == {"accuracy": 1.0}
