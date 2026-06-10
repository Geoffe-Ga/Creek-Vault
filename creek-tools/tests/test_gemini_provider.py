"""Tests for the synchronous GeminiProvider (#608).

The ``google-genai`` SDK is mocked throughout — no live calls. Covers the
consent + key gate (either ``GOOGLE_API_KEY`` or ``GEMINI_API_KEY``), model
resolution, the ``generate_content`` call, ``finish_reason`` and usage mapping,
error redaction, and a mocked end-to-end classify call.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from creek.classify.llm.base import LLMProvider
from creek.classify.llm.completion import Completion
from creek.classify.llm.consent import CLOUD_CONSENT_ENV, LEGACY_CONSENT_ENV
from creek.classify.llm.providers import (
    GeminiProvider,
    build_provider,
    provider_is_cloud,
)
from creek.config import LLMConfig

_KEY_VARS = ("GOOGLE_API_KEY", "GEMINI_API_KEY")


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Start each test with no consent and no Gemini key variables set."""
    monkeypatch.delenv(CLOUD_CONSENT_ENV, raising=False)
    monkeypatch.delenv(LEGACY_CONSENT_ENV, raising=False)
    for var in _KEY_VARS:
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def gemini_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set GOOGLE_API_KEY and cloud consent so a GeminiProvider constructs."""
    monkeypatch.setenv("GOOGLE_API_KEY", "g-not-real")
    monkeypatch.setenv(CLOUD_CONSENT_ENV, "1")


def _make_mock_genai_client(
    text: str,
    *,
    finish_reason: object = "STOP",
    prompt_tokens: int = 12,
    candidate_tokens: int = 8,
) -> MagicMock:
    """Build a mock ``genai.Client`` returning one generate_content response."""
    part = MagicMock()
    part.text = text
    content = MagicMock()
    content.parts = [part]
    candidate = MagicMock()
    candidate.content = content
    candidate.finish_reason = finish_reason
    usage = MagicMock()
    usage.prompt_token_count = prompt_tokens
    usage.candidates_token_count = candidate_tokens
    response = MagicMock()
    response.candidates = [candidate]
    response.usage_metadata = usage
    client = MagicMock()
    client.models.generate_content.return_value = response
    return client


class TestGeminiConstruction:
    """Consent + API-key gate at construction."""

    def test_raises_when_no_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Neither key var set → RuntimeError mentioning the key vars."""
        monkeypatch.setenv(CLOUD_CONSENT_ENV, "1")
        with pytest.raises(RuntimeError, match=r"GOOGLE_API_KEY|GEMINI_API_KEY"):
            GeminiProvider(LLMConfig(provider="gemini"))

    def test_constructs_with_google_api_key(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``GOOGLE_API_KEY`` satisfies the key requirement."""
        monkeypatch.setenv("GOOGLE_API_KEY", "g-not-real")
        monkeypatch.setenv(CLOUD_CONSENT_ENV, "1")
        assert GeminiProvider(LLMConfig(provider="gemini")).available is True

    def test_constructs_with_gemini_api_key(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The alternate ``GEMINI_API_KEY`` also satisfies the requirement."""
        monkeypatch.setenv("GEMINI_API_KEY", "g-not-real")
        monkeypatch.setenv(CLOUD_CONSENT_ENV, "1")
        assert GeminiProvider(LLMConfig(provider="gemini")).available is True

    def test_raises_when_consent_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Key present but no consent → RuntimeError naming Gemini."""
        monkeypatch.setenv("GOOGLE_API_KEY", "g-not-real")
        with pytest.raises(RuntimeError, match="Gemini"):
            GeminiProvider(LLMConfig(provider="gemini"))

    def test_is_cloud_and_provider_name(self, gemini_env: None) -> None:
        """Gemini is a cloud backend with a display name."""
        provider = GeminiProvider(LLMConfig(provider="gemini"))
        assert provider.is_cloud is True
        assert provider.PROVIDER_NAME == "Gemini"


class TestGeminiFactoryRegistration:
    """The factory knows the gemini backend."""

    def test_build_provider_returns_gemini(self, gemini_env: None) -> None:
        """``build_provider`` constructs a GeminiProvider for 'gemini'."""
        provider = build_provider(LLMConfig(provider="gemini"))
        assert isinstance(provider, GeminiProvider)
        assert isinstance(provider, LLMProvider)

    def test_provider_is_cloud_gemini(self) -> None:
        """'gemini' is registered as a cloud provider (no instantiation)."""
        assert provider_is_cloud("gemini") is True


class TestGeminiModelResolution:
    """Model resolution mirrors the other providers."""

    def test_default_model_literal(self) -> None:
        """The default Gemini model is the current stable flash tier (#622)."""
        assert GeminiProvider.DEFAULT_MODEL == "gemini-3.5-flash"

    def test_falls_back_when_model_unset(self, gemini_env: None) -> None:
        """An unset ``config.model`` (``None``) falls back to the Gemini default."""
        provider = GeminiProvider(LLMConfig(provider="gemini"))
        assert provider.model == GeminiProvider.DEFAULT_MODEL

    def test_honors_explicit_model(self, gemini_env: None) -> None:
        """An explicit config model is honoured."""
        provider = GeminiProvider(LLMConfig(provider="gemini", model="gemini-x"))
        assert provider.model == "gemini-x"

    def test_honors_explicit_mistral_verbatim(self, gemini_env: None) -> None:
        """An explicit ``model: mistral`` is sent verbatim, never swapped (#621)."""
        provider = GeminiProvider(LLMConfig(provider="gemini", model="mistral"))
        assert provider.model == "mistral"


class TestGeminiComplete:
    """The complete() call against a mocked SDK."""

    def test_complete_returns_completion(self, gemini_env: None) -> None:
        """A successful call normalizes to a populated Completion."""
        client = _make_mock_genai_client("hi gemini")
        with patch("google.genai.Client", return_value=client):
            provider = GeminiProvider(LLMConfig(provider="gemini"))
            result = provider.complete("a prompt")
        assert isinstance(result, Completion)
        assert result.text == "hi gemini"
        assert result.stop_reason == "end_turn"
        assert result.usage == {"input_tokens": 12, "output_tokens": 8}

    def test_complete_calls_generate_content(self, gemini_env: None) -> None:
        """The SDK is called with the resolved model and the prompt contents."""
        client = _make_mock_genai_client("x")
        with patch("google.genai.Client", return_value=client):
            provider = GeminiProvider(LLMConfig(provider="gemini"))
            provider.complete("the prompt")
        kwargs = client.models.generate_content.call_args.kwargs
        assert kwargs["model"] == GeminiProvider.DEFAULT_MODEL
        assert kwargs["contents"] == "the prompt"

    def test_complete_maps_max_tokens_string(self, gemini_env: None) -> None:
        """A string ``MAX_TOKENS`` finish reason maps to ``max_tokens``."""
        client = _make_mock_genai_client("cut", finish_reason="MAX_TOKENS")
        with patch("google.genai.Client", return_value=client):
            provider = GeminiProvider(LLMConfig(provider="gemini"))
            assert provider.complete("p").stop_reason == "max_tokens"

    def test_complete_maps_enum_finish_reason(self, gemini_env: None) -> None:
        """An enum finish reason (``.name``) maps correctly too."""
        from google.genai.types import FinishReason

        client = _make_mock_genai_client("ok", finish_reason=FinishReason.STOP)
        with patch("google.genai.Client", return_value=client):
            provider = GeminiProvider(LLMConfig(provider="gemini"))
            assert provider.complete("p").stop_reason == "end_turn"

    def test_complete_threads_max_tokens_into_config(self, gemini_env: None) -> None:
        """An explicit max_tokens flows into the generation config."""
        client = _make_mock_genai_client("x")
        with patch("google.genai.Client", return_value=client):
            provider = GeminiProvider(LLMConfig(provider="gemini"))
            provider.complete("p", max_tokens=321)
        config = client.models.generate_content.call_args.kwargs["config"]
        assert config.max_output_tokens == 321

    def test_complete_passes_system_instruction(self, gemini_env: None) -> None:
        """A system prefix flows into the config's system_instruction."""
        client = _make_mock_genai_client("x")
        with patch("google.genai.Client", return_value=client):
            provider = GeminiProvider(LLMConfig(provider="gemini"))
            provider.complete("p", system="be terse")
        config = client.models.generate_content.call_args.kwargs["config"]
        assert config.system_instruction == "be terse"

    def test_complete_redacts_sdk_errors(self, gemini_env: None) -> None:
        """SDK errors surface only the exception type name, not request state."""
        from google.genai import errors

        client = MagicMock()
        client.models.generate_content.side_effect = errors.APIError(
            503, {"error": {"message": "secret request payload"}}
        )
        with patch("google.genai.Client", return_value=client):
            provider = GeminiProvider(LLMConfig(provider="gemini"))
            with pytest.raises(RuntimeError) as exc:
                provider.complete("p")
        message = str(exc.value)
        assert "APIError" in message
        assert "secret" not in message


def test_gemini_end_to_end_classify(gemini_env: None) -> None:
    """A mocked end-to-end classify via provider=gemini returns a Completion."""
    from creek.classify.llm.orchestrator import LLMClassifier

    client = _make_mock_genai_client("response body")
    with patch("google.genai.Client", return_value=client):
        classifier = LLMClassifier(config=LLMConfig(provider="gemini"))
        completion = classifier.invoke_prompt_with_metadata("classify this")
    assert isinstance(completion, Completion)
    assert completion.text == "response body"
    assert completion.usage == {"input_tokens": 12, "output_tokens": 8}
