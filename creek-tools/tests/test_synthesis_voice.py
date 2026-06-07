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
from creek.author.skills import read_skill
from creek.author.voice import (
    VoiceAgent,
    _deterministic_body,
    _evidence_section,
    _owner_claims,
)

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


def _seed_register_skill(vault: Path, register: str, sentinel: str) -> None:
    """Seed ``creek-skills/registers/<register>.SKILL.md`` with a sentinel."""
    registers = vault / "creek-skills" / "registers"
    registers.mkdir(parents=True, exist_ok=True)
    (registers / f"{register}.SKILL.md").write_text(sentinel, encoding="utf-8")


# Dense with analytical voice-register signals (VOICE_REGISTER_SIGNALS) so the
# real Ontology specialist resolves a dominant ``analytical`` register.
_ANALYTICAL_BODY = (
    "analyze examine consider therefore evidence hypothesis framework "
    "systematically data observe"
)


def _seed_classified_fragment(vault: Path, frag_id: str, title: str, body: str) -> None:
    """Write a fragment whose body carries dense rule-classifier signal."""
    folder = vault / "01-Fragments" / "Notes"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{frag_id}.md").write_text(
        f'---\ntype: fragment\nid: {frag_id}\ntitle: "{title}"\n'
        f"source:\n  platform: journal\n  author: self\n---\n{body}\n",
        encoding="utf-8",
    )


def _mock_client(text: str) -> tuple[AuthorLLMClient, MagicMock]:
    """Return a client over a mock provider returning *text*, and the provider."""
    from creek.classify.llm.providers import AnthropicCompletion

    provider = MagicMock()
    # Return a real completion (not a MagicMock) so ``usage`` is a plain
    # dict|None that ``AuthoredDraft`` accepts — caching surfaces usage now (#474).
    provider.call_with_metadata.return_value = AnthropicCompletion(
        text=text, usage={"input_tokens": 1, "cache_read_input_tokens": 0}
    )
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
    # Structural / containment assertions (not exact equality) so a future
    # body-wrapping change can't make this brittle (#503).
    assert draft.rendered_text.strip()
    assert "Voiced in the owner's register." in draft.rendered_text
    provider.call_with_metadata.assert_called_once()


def test_all_borrowed_claims_yield_no_owner_voiced_material() -> None:
    """All-borrowed evidence leaves the owner nothing of their own to voice.

    When every claim carries an ``author_slug`` it is already attributed to
    another author and must never be voiced as the owner's words, so
    ``_owner_claims`` is empty. The deterministic body degrades to the query
    heading alone and the evidence section to the ``(no grounded evidence)``
    placeholder. This pins that intended degradation explicitly (#503).
    """
    bundle = EvidenceBundle(
        claims=[
            EvidenceClaim(
                claim="A borrowed insight",
                source_fragments=["frag-x"],
                author_slug="naval-ravikant",
            ),
        ],
    )

    assert _owner_claims(bundle) == []
    assert _deterministic_body("What do I think?", bundle) == "# What do I think?\n"
    assert "(no grounded evidence)" in _evidence_section(bundle)


def _sent_prompt(provider: MagicMock) -> str:
    """Return the full material the model saw: the cached system block + prompt.

    Caching (#474) splits the voice prompt into a static ``system`` prefix and a
    dynamic user prompt. The *content* the model sees is the union of both, so
    these assertions check the union — agnostic to which block carries a piece.
    """
    call = provider.call_with_metadata.call_args
    static = call.kwargs.get("system") or ""
    dynamic = call.args[0]
    return f"{static}\n\n{dynamic}"


def test_voice_prompt_includes_voice_core_sentinel(tmp_path: Path) -> None:
    """The voice-core SKILL.md content is loaded into the LLM voice prompt."""
    sentinel = "OWNER-VOICE-SENTINEL-XYZ"
    _seed_voice_core(tmp_path, sentinel)
    client, provider = _mock_client("voiced output")
    evidence = EvidenceBundle(
        claims=[EvidenceClaim(claim="a grounded claim", source_fragments=["f1"])]
    )

    VoiceAgent(llm_client=client).render("q", evidence, tmp_path, medium="research")

    # The static voice-skill prefix is the cached ``system`` block now.
    assert sentinel in provider.call_with_metadata.call_args.kwargs["system"]
    assert sentinel in _sent_prompt(provider)


def test_voice_prompt_includes_register_skill_sentinel(tmp_path: Path) -> None:
    """The register SKILL.md selected from the ontology reaches the voice prompt."""
    from creek.author.agents import OntologySpecialist

    sentinel = "ANALYTICAL-REGISTER-SENTINEL-XYZ"
    _seed_classified_fragment(
        tmp_path, "frag-an", "Analytical examination", _ANALYTICAL_BODY
    )
    _seed_register_skill(tmp_path, "analytical", sentinel)
    evidence = OntologySpecialist().gather("analysis", tmp_path)
    # Precondition: the ontology surfaced the analytical register the skill keys on.
    assert evidence.ontology is not None
    assert evidence.ontology.voice_registers[0].value.value == "analytical"
    client, provider = _mock_client("voiced output")

    VoiceAgent(llm_client=client).render(
        "analysis", evidence, tmp_path, medium="research"
    )

    assert sentinel in provider.call_with_metadata.call_args.kwargs["system"]
    assert sentinel in _sent_prompt(provider)


def test_voice_prompt_without_register_skips_register_skill(tmp_path: Path) -> None:
    """Evidence with no ontology resolves no register and still builds a prompt."""
    _seed_register_skill(tmp_path, "analytical", "UNWANTED-REGISTER-SENTINEL")
    client, provider = _mock_client("voiced output")
    evidence = EvidenceBundle(
        claims=[EvidenceClaim(claim="a grounded claim", source_fragments=["f1"])]
    )

    body = VoiceAgent(llm_client=client).render(
        "q", evidence, tmp_path, medium="research"
    )

    assert body == "voiced output"
    assert "UNWANTED-REGISTER-SENTINEL" not in _sent_prompt(provider)


def test_voice_prompt_honours_contract_structure(tmp_path: Path) -> None:
    """The medium contract's section order is passed to the model (#471)."""
    contract = load_medium_contract("research", tmp_path)
    client, provider = _mock_client("voiced output")
    evidence = EvidenceBundle(
        claims=[EvidenceClaim(claim="a grounded claim", source_fragments=["f1"])]
    )

    VoiceAgent(llm_client=client).render(
        "q", evidence, tmp_path, medium="research", contract=contract
    )

    assert " → ".join(contract.structure) in _sent_prompt(provider)


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

    prompt = _sent_prompt(provider)
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


def test_read_skill_skips_non_utf8_bytes(tmp_path: Path) -> None:
    """A SKILL.md with non-UTF-8 bytes is skipped (returns ""), never crashes."""
    skill = tmp_path / "voice-core.SKILL.md"
    skill.write_bytes(b"\xff\xfe not valid utf-8 \x80")

    assert read_skill(skill) == ""


def test_voice_render_survives_non_utf8_voice_core(tmp_path: Path) -> None:
    """A non-UTF-8 voice-core does not crash the LLM voice render (#501)."""
    core = tmp_path / "creek-skills" / "voice-core" / "SKILL.md"
    core.parent.mkdir(parents=True, exist_ok=True)
    core.write_bytes(b"\xff\xfe\x80 bad bytes")

    provider = MagicMock()
    completion = MagicMock()
    completion.text = "Voiced."
    provider.call_with_metadata.return_value = completion
    agent = VoiceAgent(llm_client=AuthorLLMClient(provider))
    evidence = EvidenceBundle(
        claims=[EvidenceClaim(claim="A grounded claim.", source_fragments=["frag-a"])]
    )

    body = agent.render("q", evidence, tmp_path, medium="research")

    assert body == "Voiced."
