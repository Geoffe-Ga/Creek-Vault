"""Tests for ``creek.generate.compost_verifier`` (FEAT-018 stage 2).

Covers response parsing and the :class:`LLMCompostVerifier` adapter, which is
**provider-neutral** (#667): it drives any :class:`creek.classify.llm.base.LLMProvider`
through ``complete(prompt).text``. The provider is stubbed with a minimal fake
that conforms to the protocol, keeping the unit test free of network and env-var
prerequisites. The ``_build_compost_verifier`` CLI builder is exercised for its
available / unavailable degradation paths.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from creek.classify.llm.completion import Completion
from creek.cli import _build_compost_verifier
from creek.config import CreekConfig
from creek.generate.compost_verifier import (
    CompostVerdict,
    CompostVerifierResult,
    LLMCompostVerifier,
    _parse_verifier_response,
)

if TYPE_CHECKING:
    from creek.classify.llm.base import LLMProvider

_DEFAULT_RESPONSE = "VERDICT: yes\nREASONING: ok"


class _FakeProvider:
    """A minimal :class:`LLMProvider` conformer — no network, no env vars."""

    is_cloud = False

    def __init__(
        self, response: str = _DEFAULT_RESPONSE, *, available: bool = True
    ) -> None:
        """Capture the canned completion text and the availability to report."""
        self._response = response
        self._available = available
        self.prompts: list[str] = []

    @property
    def model(self) -> str:
        """The resolved model identifier (constant for the fake)."""
        return "fake-model"

    @property
    def available(self) -> bool:
        """Whether the fake reports its prerequisites as met."""
        return self._available

    def complete(
        self, prompt: str, *, max_tokens: int | None = None, system: str | None = None
    ) -> Completion:
        """Record the prompt and return the canned completion."""
        _ = (max_tokens, system)
        self.prompts.append(prompt)
        return Completion(text=self._response)


def _as_provider(fake: _FakeProvider) -> LLMProvider:
    """Bind the fake to the protocol type (it conforms structurally)."""
    return fake


class TestParseVerifierResponse:
    """Parser for the three-line LLM response contract."""

    def test_well_formed_yes(self) -> None:
        """A well-formed ``yes`` response parses into a YES verdict."""
        text = "VERDICT: yes\nREASONING: Clear release statement."
        result = _parse_verifier_response(text)
        assert result.verdict == CompostVerdict.YES
        assert result.reasoning == "Clear release statement."

    def test_well_formed_no(self) -> None:
        """A well-formed ``no`` response parses into a NO verdict."""
        text = "VERDICT: no\nREASONING: Author is on a planned break."
        result = _parse_verifier_response(text)
        assert result.verdict == CompostVerdict.NO

    def test_well_formed_ambiguous(self) -> None:
        """A well-formed ``ambiguous`` response parses into AMBIGUOUS."""
        text = "VERDICT: ambiguous\nREASONING: Could read either way."
        result = _parse_verifier_response(text)
        assert result.verdict == CompostVerdict.AMBIGUOUS

    def test_case_insensitive(self) -> None:
        """Verdict and reasoning labels match case-insensitively."""
        text = "verdict: YES\nreasoning: ok"
        result = _parse_verifier_response(text)
        assert result.verdict == CompostVerdict.YES
        assert result.reasoning == "ok"

    def test_extra_whitespace_tolerated(self) -> None:
        """Leading/trailing whitespace on lines is stripped."""
        text = "   VERDICT:   no   \n   REASONING:   nope   "
        result = _parse_verifier_response(text)
        assert result.verdict == CompostVerdict.NO
        assert result.reasoning == "nope"

    def test_missing_reasoning_uses_fallback(self) -> None:
        """A response with only ``VERDICT:`` gets a placeholder reason."""
        text = "VERDICT: yes"
        result = _parse_verifier_response(text)
        assert result.verdict == CompostVerdict.YES
        assert result.reasoning == "(no reason)"

    def test_unparseable_routes_to_ambiguous(self) -> None:
        """A response with no VERDICT line is treated as AMBIGUOUS."""
        text = "I'm sorry, I can't help with that."
        result = _parse_verifier_response(text)
        assert result.verdict == CompostVerdict.AMBIGUOUS
        assert "Unparseable" in result.reasoning

    def test_invalid_verdict_routes_to_ambiguous(self) -> None:
        """A VERDICT line with a non-canonical value is treated as AMBIGUOUS."""
        text = "VERDICT: maybe\nREASONING: not sure"
        result = _parse_verifier_response(text)
        assert result.verdict == CompostVerdict.AMBIGUOUS


class TestLLMCompostVerifier:
    """End-to-end behaviour of the provider-neutral verifier."""

    def test_verify_returns_parsed_result(self) -> None:
        """``verify`` drives the provider via ``complete`` and parses the result."""
        provider = _FakeProvider("VERDICT: yes\nREASONING: clear release")
        verifier = LLMCompostVerifier(_as_provider(provider))
        result = verifier.verify(title="Letting go", body="It's over.")
        assert isinstance(result, CompostVerifierResult)
        assert result.verdict == CompostVerdict.YES
        assert result.reasoning == "clear release"

    def test_verify_includes_title_and_body_in_prompt(self) -> None:
        """The prompt embeds both fields verbatim for verifier context."""
        provider = _FakeProvider("VERDICT: no\nREASONING: x")
        verifier = LLMCompostVerifier(_as_provider(provider))
        verifier.verify(title="My title", body="My body text.")
        assert len(provider.prompts) == 1
        sent = provider.prompts[0]
        assert "My title" in sent
        assert "My body text." in sent

    def test_verify_handles_empty_title(self) -> None:
        """An empty title is replaced with a placeholder so the prompt is valid."""
        provider = _FakeProvider("VERDICT: yes\nREASONING: ok")
        verifier = LLMCompostVerifier(_as_provider(provider))
        verifier.verify(title="", body="body")
        assert "(untitled)" in provider.prompts[0]

    def test_malformed_response_routes_to_ambiguous(self) -> None:
        """A malformed LLM response yields AMBIGUOUS without raising."""
        provider = _FakeProvider("hello there friend")
        verifier = LLMCompostVerifier(_as_provider(provider))
        result = verifier.verify(title="t", body="b")
        assert result.verdict == CompostVerdict.AMBIGUOUS


class TestBuildCompostVerifier:
    """``_build_compost_verifier`` is provider-neutral and degrades gracefully."""

    def test_returns_verifier_when_provider_available(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An available (non-Anthropic) provider yields a working verifier."""
        fake = _FakeProvider("VERDICT: no\nREASONING: keep")
        monkeypatch.setattr("creek.classify.llm.build_provider", lambda _c: fake)

        verifier = _build_compost_verifier(CreekConfig())

        assert isinstance(verifier, LLMCompostVerifier)
        # Drives the configured provider through the neutral ``complete`` path.
        assert verifier.verify(title="t", body="b").verdict == CompostVerdict.NO
        assert fake.prompts

    def test_none_when_build_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A cloud provider that raises (no key) degrades to embedding-only."""

        def _raise(_c: object) -> _FakeProvider:
            msg = "ANTHROPIC_API_KEY not set"
            raise RuntimeError(msg)

        monkeypatch.setattr("creek.classify.llm.build_provider", _raise)
        assert _build_compost_verifier(CreekConfig()) is None

    def test_none_when_provider_unavailable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A local provider that constructs but is unreachable degrades too."""
        fake = _FakeProvider(available=False)
        monkeypatch.setattr("creek.classify.llm.build_provider", lambda _c: fake)
        assert _build_compost_verifier(CreekConfig()) is None


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("VERDICT: yes\nREASONING: a", CompostVerdict.YES),
        ("VERDICT: no\nREASONING: b", CompostVerdict.NO),
        ("VERDICT: ambiguous\nREASONING: c", CompostVerdict.AMBIGUOUS),
    ],
)
def test_verdict_parsing_round_trip(text: str, expected: CompostVerdict) -> None:
    """Parametrised round-trip for the three canonical verdict strings."""
    assert _parse_verifier_response(text).verdict == expected
