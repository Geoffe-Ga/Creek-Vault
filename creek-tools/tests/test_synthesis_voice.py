"""Tests for grounded synthesis and voice rendering (FEAT-041, #471).

These pin the two halves the Writing Desk gained in #471: the conductor's
``synthesize`` drops ungrounded claims so every retained claim traces to its
source fragments, and the :class:`~creek.author.voice.VoiceAgent` renders the
grounded evidence in the owner's voice through the injected
:class:`~creek.author.client.AuthorLLMClient` and the vault's ``creek-skills``
voice-skill stack. No live network — the LLM is a ``MagicMock`` provider.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import MagicMock

from creek.author.client import AuthorLLMClient
from creek.author.conductor import Conductor, build_default_conductor
from creek.author.contracts import load_medium_contract
from creek.author.models import EvidenceBundle, EvidenceClaim
from creek.author.reflection import ReflectionNode
from creek.author.voice import VoiceAgent

if TYPE_CHECKING:
    from pathlib import Path


def _seed_fragment(vault: Path, frag_id: str, title: str) -> None:
    """Write a minimal owner fragment so the real specialists have a corpus."""
    folder = vault / "01-Fragments" / "Notes"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{frag_id}.md").write_text(
        f'---\ntype: fragment\nid: {frag_id}\ntitle: "{title}"\n'
        f"source:\n  platform: journal\n  author: self\n---\nbody\n",
        encoding="utf-8",
    )


def _seed_voice_core(vault: Path, sentinel: str) -> None:
    """Seed ``creek-skills/voice-core/SKILL.md`` with a sentinel marker."""
    core = vault / "creek-skills" / "voice-core"
    core.mkdir(parents=True, exist_ok=True)
    (core / "SKILL.md").write_text(sentinel, encoding="utf-8")


def _mock_client(text: str) -> tuple[AuthorLLMClient, MagicMock]:
    """Return a client over a mock provider returning *text*, and the provider."""
    provider = MagicMock()
    completion = MagicMock()
    completion.text = text
    provider.call_with_metadata.return_value = completion
    return AuthorLLMClient(provider), provider


def test_synthesize_drops_ungrounded_claims() -> None:
    """``synthesize`` keeps only claims that trace to source fragments."""
    grounded = EvidenceClaim(claim="grounded one", source_fragments=["f1"])
    ungrounded = EvidenceClaim(claim="floating claim", source_fragments=[])
    conductor = build_default_conductor(max_rounds=1)

    merged = conductor.synthesize([EvidenceBundle(claims=[grounded, ungrounded])])

    assert [c.claim for c in merged.claims] == ["grounded one"]
    assert all(c.source_fragments for c in merged.claims)
    assert "floating claim" not in {c.claim for c in merged.claims}


def test_synthesize_carries_first_ontology() -> None:
    """``synthesize`` carries the first non-None ontology through unchanged."""
    from creek.author.models import OntologyAnalysis

    analysis = OntologyAnalysis(overall_confidence=0.5)
    conductor = build_default_conductor(max_rounds=1)

    merged = conductor.synthesize(
        [
            EvidenceBundle(claims=[EvidenceClaim(claim="a", source_fragments=["f1"])]),
            EvidenceBundle(
                claims=[EvidenceClaim(claim="b", source_fragments=["f2"])],
                ontology=analysis,
            ),
        ]
    )

    assert merged.ontology is analysis


def test_run_grounded_and_voiced_end_to_end(tmp_path: Path) -> None:
    """A full run yields non-empty per-claim provenance and the voiced body."""
    _seed_fragment(tmp_path, "frag-a", "F6 Pluralism and community")
    _seed_fragment(tmp_path, "frag-b", "Agency and momentum")
    client, provider = _mock_client("Voiced in the owner's register.")
    contract = load_medium_contract("research", tmp_path)
    conductor = Conductor(
        specialists=build_default_conductor(max_rounds=1).specialists,
        voice=VoiceAgent(llm_client=client),
        reflection=ReflectionNode(),
        max_rounds=1,
        llm_client=client,
        contract=contract,
    )

    draft = conductor.run(medium="research", query="What is F6?", vault=tmp_path)

    assert draft.provenance
    assert all(entry.fragment_ids for entry in draft.provenance)
    assert draft.rendered_text == "Voiced in the owner's register."
    provider.call_with_metadata.assert_called_once()


def test_voice_prompt_includes_voice_core_sentinel(tmp_path: Path) -> None:
    """The voice-core SKILL.md content is loaded into the LLM voice prompt."""
    sentinel = "OWNER-VOICE-SENTINEL-XYZ"
    _seed_voice_core(tmp_path, sentinel)
    client, provider = _mock_client("voiced output")
    evidence = EvidenceBundle(
        claims=[EvidenceClaim(claim="a grounded claim", source_fragments=["f1"])]
    )

    VoiceAgent(llm_client=client).render("q", evidence, tmp_path, medium="research")

    prompt = provider.call_with_metadata.call_args.args[0]
    assert sentinel in prompt


def test_voice_excludes_borrowed_authors_from_owner_material(tmp_path: Path) -> None:
    """A borrowed (``author_slug``) claim is not voiced as the owner's words."""
    client, provider = _mock_client("voiced output")
    owner = EvidenceClaim(claim="my own observation", source_fragments=["f1"])
    borrowed = EvidenceClaim(
        claim="a borrowed maxim",
        source_fragments=["f2"],
        author_slug="some-author",
    )
    evidence = EvidenceBundle(claims=[owner, borrowed])

    VoiceAgent(llm_client=client).render("q", evidence, tmp_path, medium="research")

    prompt = provider.call_with_metadata.call_args.args[0]
    assert "my own observation" in prompt
    assert "a borrowed maxim" not in prompt


def test_voice_without_client_is_deterministic() -> None:
    """With no client the render returns a non-empty deterministic body."""
    evidence = EvidenceBundle(
        claims=[EvidenceClaim(claim="a claim", source_fragments=["f1"])]
    )

    body = VoiceAgent().render("q", evidence)

    assert body.strip()
    assert "a claim" in body


def test_voice_falls_back_when_client_returns_empty(tmp_path: Path) -> None:
    """An empty LLM completion falls back to the deterministic body."""
    client, _provider = _mock_client("   ")
    evidence = EvidenceBundle(
        claims=[EvidenceClaim(claim="a claim", source_fragments=["f1"])]
    )

    body = VoiceAgent(llm_client=client).render("q", evidence, tmp_path)

    assert "a claim" in body
