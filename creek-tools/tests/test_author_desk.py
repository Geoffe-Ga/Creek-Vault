"""Smoke tests for the Creek Writing Desk author skeleton (FEAT-041, #455).

Every surface here is a typed *stub*: the conductor drives stub specialists,
a stub voice agent, and a stub reflection node, returning a shaped
:class:`~creek.author.AuthoredDraft`. These tests pin the *shapes* and the
orchestration contract — not real retrieval, synthesis, or judging (those are
issues #460/#463/#467/#471).
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest

from creek.config import LLMConfig

if TYPE_CHECKING:
    from pathlib import Path

from creek.author import (
    AuthoredDraft,
    AuthorLLMClient,
    Conductor,
    EvidenceBundle,
    build_default_conductor,
    run_author,
)
from creek.author.agents import (
    GraphSpecialist,
    OntologySpecialist,
    RetrievalSpecialist,
)
from creek.author.models import EvidenceClaim
from creek.author.reflection import ReflectionNode
from creek.author.voice import VoiceAgent


def test_run_author_returns_shaped_draft(tmp_path: Path) -> None:
    """``run_author`` returns an ``AuthoredDraft`` with mock provenance + verdict."""
    draft = run_author(medium="research", query="What is F6 Pluralism?", vault=tmp_path)

    assert isinstance(draft, AuthoredDraft)
    assert draft.medium == "research"
    assert draft.body.strip()
    assert draft.provenance  # non-empty mock provenance
    assert draft.verdict in {"PASS", "REVISE", "ESCALATE"}
    assert draft.rounds >= 1


@pytest.mark.parametrize(
    "specialist",
    [GraphSpecialist(), RetrievalSpecialist(), OntologySpecialist()],
)
def test_each_specialist_returns_structured_evidence(
    specialist: GraphSpecialist | RetrievalSpecialist | OntologySpecialist,
    tmp_path: Path,
) -> None:
    """Each stub specialist returns structured evidence, never free prose."""
    bundle = specialist.gather("q", tmp_path)

    assert isinstance(bundle, EvidenceBundle)
    assert bundle.claims
    for claim in bundle.claims:
        assert claim.claim.strip()
        assert claim.source_fragments  # claims are traced to fragments


def test_conductor_plan_lists_pipeline() -> None:
    """``plan`` names the specialist steps followed by synthesize/voice/reflect."""
    conductor = build_default_conductor(max_rounds=3)

    assert conductor.plan() == [
        "graph",
        "retrieval",
        "ontology",
        "synthesize",
        "voice",
        "reflect",
    ]


def test_voice_stub_renders_body_from_evidence(tmp_path: Path) -> None:
    """The stub voice agent renders a non-empty body from the evidence."""
    evidence = GraphSpecialist().gather("q", tmp_path)

    body = VoiceAgent().render("q", evidence)

    assert body.strip()


def test_reflection_passes_grounded_and_escalates_ungrounded(tmp_path: Path) -> None:
    """The stub reflection node passes grounded drafts and escalates empty ones."""
    evidence = GraphSpecialist().gather("q", tmp_path)

    assert ReflectionNode().review("a real body", evidence) == "PASS"
    assert ReflectionNode().review("   ", evidence) == "ESCALATE"
    assert ReflectionNode().review("body", EvidenceBundle()) == "ESCALATE"


def test_conductor_respects_max_rounds(tmp_path: Path) -> None:
    """A REVISE verdict loops up to ``max_rounds`` then returns that verdict."""
    revising = MagicMock()
    revising.review.return_value = "REVISE"
    conductor = Conductor(
        specialists=[GraphSpecialist()],
        voice=VoiceAgent(),
        reflection=revising,
        max_rounds=2,
    )

    draft = conductor.run(medium="research", query="q", vault=tmp_path)

    assert draft.rounds == 2
    assert draft.verdict == "REVISE"
    assert revising.review.call_count == 2


def test_evidence_bundle_dedups_source_fragments() -> None:
    """``all_source_fragments`` is an order-preserving, deduplicated union."""
    bundle = EvidenceBundle(
        claims=[
            EvidenceClaim(claim="a", source_fragments=["f1", "f2"]),
            EvidenceClaim(claim="b", source_fragments=["f2", "f3"]),
        ]
    )

    assert bundle.all_source_fragments() == ["f1", "f2", "f3"]


def test_conductor_synthesize_merges_bundles(tmp_path: Path) -> None:
    """The ``synthesize`` step merges every specialist bundle into one."""
    conductor = build_default_conductor(max_rounds=1)
    bundles = [s.gather("q", tmp_path) for s in conductor.specialists]

    merged = conductor.synthesize(bundles)

    expected = sum(len(b.claims) for b in bundles)
    assert len(merged.claims) == expected
    assert merged.claims  # non-empty


def test_plan_steps_each_correspond_to_a_real_call(tmp_path: Path) -> None:
    """Every advertised plan step is exercised by a real run (no ghost steps)."""
    conductor = build_default_conductor(max_rounds=1)
    # ``synthesize`` must exist and run, not just appear in ``plan()``.
    assert "synthesize" in conductor.plan()
    evidence = conductor.gather_evidence("q", tmp_path)
    assert evidence.claims  # gather_evidence runs the synthesize step


def test_require_supported_medium_returns_validated_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Validation echoes back the *requested* medium, not a hard-coded literal."""
    from creek.author import conductor as conductor_mod

    monkeypatch.setattr(
        conductor_mod, "SUPPORTED_MEDIUMS", frozenset({"research", "essay"})
    )
    assert conductor_mod._require_supported_medium("essay") == "essay"


def test_run_author_rejects_unknown_medium(tmp_path: Path) -> None:
    """Only the ``research`` medium is wired; others raise a clear error."""
    with pytest.raises(ValueError, match="research"):
        run_author(medium="book-report", query="q", vault=tmp_path)


def test_author_llm_client_delegates_to_provider() -> None:
    """The thin client wrapper delegates to the provider with no live network."""
    provider = MagicMock()
    completion = MagicMock()
    completion.text = "hello"
    provider.call_with_metadata.return_value = completion

    client = AuthorLLMClient(provider)
    result = client.complete("prompt", max_tokens=10)

    assert result == "hello"
    provider.call_with_metadata.assert_called_once_with("prompt", max_tokens=10)


def test_author_llm_client_from_config_builds_provider() -> None:
    """``from_config`` constructs a provider from config (model id from config)."""
    with patch("creek.author.client.AnthropicProvider") as mock_provider_cls:
        client = AuthorLLMClient.from_config(LLMConfig(provider="anthropic"))

    assert isinstance(client, AuthorLLMClient)
    mock_provider_cls.assert_called_once()
