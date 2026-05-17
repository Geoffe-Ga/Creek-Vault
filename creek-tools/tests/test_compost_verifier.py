"""Tests for ``creek.generate.compost_verifier`` (FEAT-018 stage 2).

Covers response parsing and the :class:`LLMCompostVerifier` adapter
that wraps :class:`creek.classify.llm.AnthropicProvider`. The
:class:`AnthropicProvider` is stubbed with a minimal fake to keep the
unit test free of network and env-var prerequisites.
"""

from __future__ import annotations

import pytest

from creek.generate.compost_verifier import (
    CompostVerdict,
    CompostVerifierResult,
    LLMCompostVerifier,
    _parse_verifier_response,
)


class _FakeProvider:
    """Stand-in for :class:`AnthropicProvider` exposing only ``call``."""

    def __init__(self, response: str) -> None:
        self.response = response
        self.calls: list[str] = []

    def call(self, prompt: str) -> str:
        """Record the prompt and return the canned response."""
        self.calls.append(prompt)
        return self.response


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
    """End-to-end behaviour of the LLM-backed verifier."""

    def test_verify_returns_parsed_result(self) -> None:
        """``verify`` sends a prompt and returns the parsed response."""
        provider = _FakeProvider("VERDICT: yes\nREASONING: clear release")
        verifier = LLMCompostVerifier(provider)  # type: ignore[arg-type]
        result = verifier.verify(title="Letting go", body="It's over.")
        assert isinstance(result, CompostVerifierResult)
        assert result.verdict == CompostVerdict.YES
        assert result.reasoning == "clear release"

    def test_verify_includes_title_and_body_in_prompt(self) -> None:
        """The prompt embeds both fields verbatim for verifier context."""
        provider = _FakeProvider("VERDICT: no\nREASONING: x")
        verifier = LLMCompostVerifier(provider)  # type: ignore[arg-type]
        verifier.verify(title="My title", body="My body text.")
        assert len(provider.calls) == 1
        sent = provider.calls[0]
        assert "My title" in sent
        assert "My body text." in sent

    def test_verify_handles_empty_title(self) -> None:
        """An empty title is replaced with a placeholder so the prompt is valid."""
        provider = _FakeProvider("VERDICT: yes\nREASONING: ok")
        verifier = LLMCompostVerifier(provider)  # type: ignore[arg-type]
        verifier.verify(title="", body="body")
        assert "(untitled)" in provider.calls[0]

    def test_malformed_response_routes_to_ambiguous(self) -> None:
        """A malformed LLM response yields AMBIGUOUS without raising."""
        provider = _FakeProvider("hello there friend")
        verifier = LLMCompostVerifier(provider)  # type: ignore[arg-type]
        result = verifier.verify(title="t", body="b")
        assert result.verdict == CompostVerdict.AMBIGUOUS


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
