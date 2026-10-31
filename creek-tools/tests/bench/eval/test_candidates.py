"""The candidate-generation seam: prompt in, text out, a ``Candidate`` back.

Every test here is hermetic. A source is a fake object with one method, the
routed path is exercised through an injected provider factory, and nothing
opens a socket. The corpus is the synthetic one #1850 ships.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import pytest

from creek.classify.llm.completion import Completion
from creek.config import LLMConfig
from creek_mcp.bench.eval import candidates
from creek_mcp.bench.eval.blinding import blind
from creek_mcp.bench.eval.candidates import (
    CANDIDATE_ROLE,
    CandidateUnavailableError,
    FakeCandidateSource,
    ProviderSource,
    build_prompt,
    generate_candidate,
    guard_refusals,
    parse_citations,
    routed_source,
    unknown_citations,
)
from creek_mcp.bench.eval.dimensions import (
    CARE_PROBE_TEXT,
    DEFAULT_INSTRUCTIONS,
    DeskInstructions,
    expected_refusals,
    obedience_checks,
    score_grounding_accuracy,
)
from creek_mcp.bench.eval.rubric import RefusalProbe
from creek_mcp.bench.eval.voice_corpus import (
    VOICE_ENTRIES,
    VoiceCorpus,
    build_voice_corpus,
)
from tests.bench.eval.conftest import IN_VOICE_TEXT

if TYPE_CHECKING:
    from pathlib import Path

_CATALOG = ("gpt-oss-120b", "glm-5.3", "glm-5.3-flash")
"""Example chat-model names from the owner's catalog; never dialled here."""


@dataclass
class RecordingSource:
    """A source that records every prompt and answers with fixed text."""

    text: str
    prompts: list[str] = field(default_factory=list)

    def generate(self, prompt: str) -> str:
        """Record *prompt* and return :attr:`text`."""
        self.prompts.append(prompt)
        return self.text


@dataclass
class StubProvider:
    """An ``LLMProvider`` stand-in that never touches a socket."""

    text: str = "I went out. [[voice-0000]]"
    available: bool = True
    is_cloud: bool = True
    model: str = "stub"
    calls: list[tuple[str, int | None]] = field(default_factory=list)

    def complete(
        self,
        prompt: str,
        *,
        max_tokens: int | None = None,
        system: str | None = None,
    ) -> Completion:
        """Record the call and return the canned completion."""
        del system
        self.calls.append((prompt, max_tokens))
        return Completion(text=self.text)


@pytest.fixture
def corpus(tmp_path: Path) -> VoiceCorpus:
    """The synthetic corpus, built under the pytest temp dir."""
    return build_voice_corpus(tmp_path / "voice")


def _cited(*ids: str) -> str:
    """Return in-voice text citing *ids* in the generator's citation syntax."""
    return IN_VOICE_TEXT + " " + " ".join(f"[[{ident}]]" for ident in ids)


def test_the_prompt_carries_every_entry_under_its_fragment_id(
    corpus: VoiceCorpus,
) -> None:
    """The model is shown exactly the corpus it will be scored against."""
    prompt = build_prompt(corpus, DEFAULT_INSTRUCTIONS)
    for ident, body in zip(corpus.fragment_ids, VOICE_ENTRIES, strict=False):
        assert f"[{ident}] {body}" in prompt


def test_the_prompt_states_every_writing_desk_instruction(
    corpus: VoiceCorpus,
) -> None:
    """Obedience is scored on instructions the model was actually given."""
    instructions = DeskInstructions(
        min_words=50,
        max_words=120,
        required_headings=("## Morning",),
        banned_phrases=("tapestry",),
    )
    prompt = build_prompt(corpus, instructions)
    assert "between 50 and 120 words" in prompt
    assert "## Morning" in prompt
    assert '"tapestry"' in prompt


def test_the_prompt_never_carries_a_refusal_probe(corpus: VoiceCorpus) -> None:
    """The care probe is read by the guard alone, never sent to a model."""
    assert CARE_PROBE_TEXT not in build_prompt(corpus, DEFAULT_INSTRUCTIONS)


def test_citations_are_parsed_in_order_without_repeats() -> None:
    """Double-bracketed ids are citations; anything else is prose."""
    text = "a [[voice-0002]] b [[voice-0001]] c [[voice-0002]] d [voice-0003]"
    assert parse_citations(text) == ("voice-0002", "voice-0001")


def test_generate_candidate_builds_a_candidate_from_one_source_call(
    corpus: VoiceCorpus,
) -> None:
    """One prompt in, one text out, and the identity stays harness-side."""
    source = RecordingSource(_cited("voice-0001", "voice-0003"))
    candidate = generate_candidate(corpus, DEFAULT_INSTRUCTIONS, "glm-5.3", source)
    assert source.prompts == [build_prompt(corpus, DEFAULT_INSTRUCTIONS)]
    assert candidate.model_tag == "glm-5.3"
    assert candidate.text == source.text
    assert candidate.citations == ("voice-0001", "voice-0003")


def test_a_citation_the_corpus_does_not_contain_scores_zero(
    corpus: VoiceCorpus,
) -> None:
    """An invented citation earns nothing; the real one still earns its share."""
    mixed = generate_candidate(
        corpus,
        DEFAULT_INSTRUCTIONS,
        "gpt-oss-120b",
        RecordingSource(_cited("voice-0000", "voice-9999")),
    )
    invented = generate_candidate(
        corpus,
        DEFAULT_INSTRUCTIONS,
        "gpt-oss-120b",
        RecordingSource(_cited("voice-9999")),
    )
    assert mixed.citations == ("voice-0000", "voice-9999")
    assert unknown_citations(mixed.citations, corpus) == ("voice-9999",)
    assert unknown_citations(invented.citations, corpus) == ("voice-9999",)
    assert score_grounding_accuracy(corpus, blind([mixed], seed=0)).score == 0.5
    assert score_grounding_accuracy(corpus, blind([invented], seed=0)).score == 0.0


def test_refusals_come_from_the_guard_path_not_the_model(
    corpus: VoiceCorpus,
) -> None:
    """Whatever the model says, the refusal record is the pipeline's guards."""
    source = RecordingSource("I refuse. I refuse. I refuse.")
    candidate = generate_candidate(corpus, DEFAULT_INSTRUCTIONS, "glm-5.3", source)
    assert dict(candidate.refusals) == expected_refusals()
    assert set(candidate.refusals) == set(RefusalProbe)
    assert all(CARE_PROBE_TEXT not in prompt for prompt in source.prompts)


def test_a_guard_that_stops_refusing_moves_the_record(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The care refusal is read off the real guard, not written down."""
    monkeypatch.setattr(candidates, "acute_distress_guard", lambda _text: None)
    refusals = guard_refusals()
    assert refusals[RefusalProbe.CARE_GUARD] is False
    assert refusals[RefusalProbe.INTIMATE_FRAGMENT] is True


def test_the_fake_source_is_deterministic_and_grounded(
    corpus: VoiceCorpus,
) -> None:
    """Fake mode is reproducible and only ever cites what it was shown."""
    prompt = build_prompt(corpus, DEFAULT_INSTRUCTIONS)
    for tag in _CATALOG:
        first = FakeCandidateSource(tag).generate(prompt)
        assert first == FakeCandidateSource(tag).generate(prompt)
        cited = parse_citations(first)
        assert cited
        assert not unknown_citations(cited, corpus)


def test_the_fake_source_obeys_the_default_instructions(
    corpus: VoiceCorpus,
) -> None:
    """A fake candidate passes every obedience component, so fake runs read clean."""
    candidate = generate_candidate(
        corpus, DEFAULT_INSTRUCTIONS, "glm-5.3-flash", FakeCandidateSource("x")
    )
    checks = obedience_checks(blind([candidate], seed=0), DEFAULT_INSTRUCTIONS)
    assert all(all(one.values()) for one in checks.values())


def test_the_fake_source_needs_entries_to_cite() -> None:
    """A prompt with no entries is a harness bug, refused rather than faked."""
    with pytest.raises(CandidateUnavailableError):
        FakeCandidateSource("x").generate("no entries here")


def test_a_provider_source_returns_the_completion_text() -> None:
    """The live adapter is one completion call, bounded by max_tokens."""
    provider = StubProvider()
    assert ProviderSource(provider, max_tokens=321).generate("p") == provider.text
    assert provider.calls == [("p", 321)]


def test_an_unavailable_provider_is_refused_before_any_call() -> None:
    """Unmet prerequisites refuse; they never become an empty candidate."""
    provider = StubProvider(available=False)
    with pytest.raises(CandidateUnavailableError):
        ProviderSource(provider, max_tokens=10).generate("p")
    assert provider.calls == []


def test_a_routed_source_resolves_through_the_router() -> None:
    """The live path goes through the shared router, keeping the model tag."""
    seen: list[LLMConfig] = []
    provider = StubProvider()

    def _factory(config: LLMConfig) -> StubProvider:
        seen.append(config)
        return provider

    config = LLMConfig(provider="openai", model="gpt-oss-120b")
    source = routed_source(config, max_tokens=64, provider_factory=_factory)
    assert source.generate("p") == provider.text
    assert [one.model for one in seen] == ["gpt-oss-120b"]
    assert CANDIDATE_ROLE
