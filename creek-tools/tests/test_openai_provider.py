"""Tests for the synchronous OpenAIProvider (#607).

The ``openai`` SDK is mocked throughout — no live calls. Covers the consent +
key gate, model resolution, the chat-completions call, ``finish_reason`` and
usage mapping, error redaction, and a mocked end-to-end classify call.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from creek.classify.llm.base import LLMProvider
from creek.classify.llm.completion import Completion
from creek.classify.llm.consent import CLOUD_CONSENT_ENV, LEGACY_CONSENT_ENV
from creek.classify.llm.providers import (
    OpenAIProvider,
    build_provider,
    provider_is_cloud,
)
from creek.config import LLMConfig


@pytest.fixture(autouse=True)
def _clear_consent(monkeypatch: pytest.MonkeyPatch) -> None:
    """Start each test with neither consent variable set."""
    monkeypatch.delenv(CLOUD_CONSENT_ENV, raising=False)
    monkeypatch.delenv(LEGACY_CONSENT_ENV, raising=False)


@pytest.fixture
def openai_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set the API key and cloud consent so an OpenAIProvider constructs."""
    monkeypatch.setenv(OpenAIProvider.API_KEY_ENV, "sk-openai-not-real")
    monkeypatch.setenv(CLOUD_CONSENT_ENV, "1")


def _make_mock_openai_client(
    content: str,
    *,
    finish_reason: str = "stop",
    prompt_tokens: int = 11,
    completion_tokens: int = 7,
) -> MagicMock:
    """Build a mock ``openai.OpenAI`` client returning one chat completion."""
    message = MagicMock()
    message.content = content
    choice = MagicMock()
    choice.message = message
    choice.finish_reason = finish_reason
    usage = MagicMock()
    usage.prompt_tokens = prompt_tokens
    usage.completion_tokens = completion_tokens
    response = MagicMock()
    response.choices = [choice]
    response.usage = usage
    client = MagicMock()
    client.chat.completions.create.return_value = response
    return client


class TestOpenAIProviderConstruction:
    """Consent + API-key gate at construction."""

    def test_raises_when_api_key_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """No ``OPENAI_API_KEY`` → RuntimeError naming the variable."""
        monkeypatch.delenv(OpenAIProvider.API_KEY_ENV, raising=False)
        monkeypatch.setenv(CLOUD_CONSENT_ENV, "1")
        with pytest.raises(RuntimeError, match=OpenAIProvider.API_KEY_ENV):
            OpenAIProvider(LLMConfig(provider="openai"))

    def test_raises_when_consent_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Key present but no consent → RuntimeError naming OpenAI."""
        monkeypatch.setenv(OpenAIProvider.API_KEY_ENV, "sk-openai-not-real")
        with pytest.raises(RuntimeError, match="OpenAI"):
            OpenAIProvider(LLMConfig(provider="openai"))

    def test_constructs_with_key_and_consent(self, openai_env: None) -> None:
        """Key + consent → constructs and reports available."""
        provider = OpenAIProvider(LLMConfig(provider="openai"))
        assert provider.available is True

    def test_is_cloud_and_provider_name(self, openai_env: None) -> None:
        """OpenAI is a cloud backend with a display name."""
        provider = OpenAIProvider(LLMConfig(provider="openai"))
        assert provider.is_cloud is True
        assert provider.PROVIDER_NAME == "OpenAI"


class TestOpenAIFactoryRegistration:
    """The factory knows the openai backend."""

    def test_build_provider_returns_openai(self, openai_env: None) -> None:
        """``build_provider`` constructs an OpenAIProvider for 'openai'."""
        provider = build_provider(LLMConfig(provider="openai"))
        assert isinstance(provider, OpenAIProvider)
        assert isinstance(provider, LLMProvider)

    def test_provider_is_cloud_openai(self) -> None:
        """'openai' is registered as a cloud provider (no instantiation)."""
        assert provider_is_cloud("openai") is True


class TestOpenAIModelResolution:
    """Model resolution mirrors the Anthropic unset-fallback logic."""

    def test_default_model_literal(self) -> None:
        """The default OpenAI model is the current balanced tier."""
        assert OpenAIProvider.DEFAULT_MODEL == "gpt-5.4"

    def test_falls_back_when_model_unset(self, openai_env: None) -> None:
        """An unset ``config.model`` (``None``) falls back to the OpenAI default."""
        provider = OpenAIProvider(LLMConfig(provider="openai"))
        assert provider.model == OpenAIProvider.DEFAULT_MODEL

    def test_honors_explicit_model(self, openai_env: None) -> None:
        """An explicit config model is honoured."""
        provider = OpenAIProvider(LLMConfig(provider="openai", model="gpt-x"))
        assert provider.model == "gpt-x"

    def test_honors_explicit_mistral_verbatim(self, openai_env: None) -> None:
        """An explicit ``model: mistral`` is sent verbatim, never swapped (#621)."""
        provider = OpenAIProvider(LLMConfig(provider="openai", model="mistral"))
        assert provider.model == "mistral"


class TestOpenAIComplete:
    """The complete() call against a mocked SDK."""

    def test_complete_returns_completion(self, openai_env: None) -> None:
        """A successful call normalizes to a populated Completion."""
        client = _make_mock_openai_client("hello world")
        with patch("openai.OpenAI", return_value=client):
            provider = OpenAIProvider(LLMConfig(provider="openai"))
            result = provider.complete("a prompt")
        assert isinstance(result, Completion)
        assert result.text == "hello world"
        assert result.stop_reason == "end_turn"
        assert result.usage == {"input_tokens": 11, "output_tokens": 7}

    def test_complete_calls_chat_completions(self, openai_env: None) -> None:
        """The SDK is called with the resolved model and the prompt."""
        client = _make_mock_openai_client("x")
        with patch("openai.OpenAI", return_value=client):
            provider = OpenAIProvider(LLMConfig(provider="openai"))
            provider.complete("the prompt")
        kwargs = client.chat.completions.create.call_args.kwargs
        assert kwargs["model"] == OpenAIProvider.DEFAULT_MODEL
        assert {"role": "user", "content": "the prompt"} in kwargs["messages"]

    def test_complete_maps_length_to_max_tokens(self, openai_env: None) -> None:
        """``finish_reason='length'`` maps to the ``max_tokens`` stop reason."""
        client = _make_mock_openai_client("cut", finish_reason="length")
        with patch("openai.OpenAI", return_value=client):
            provider = OpenAIProvider(LLMConfig(provider="openai"))
            result = provider.complete("p")
        assert result.stop_reason == "max_tokens"

    def test_complete_threads_max_tokens(self, openai_env: None) -> None:
        """An explicit max_tokens is forwarded to the SDK."""
        client = _make_mock_openai_client("x")
        with patch("openai.OpenAI", return_value=client):
            provider = OpenAIProvider(LLMConfig(provider="openai"))
            provider.complete("p", max_tokens=256)
        assert client.chat.completions.create.call_args.kwargs["max_tokens"] == 256

    def test_complete_includes_system_message(self, openai_env: None) -> None:
        """A system prefix is sent as a leading system message."""
        client = _make_mock_openai_client("x")
        with patch("openai.OpenAI", return_value=client):
            provider = OpenAIProvider(LLMConfig(provider="openai"))
            provider.complete("p", system="be terse")
        messages = client.chat.completions.create.call_args.kwargs["messages"]
        assert messages[0] == {"role": "system", "content": "be terse"}
        assert messages[-1] == {"role": "user", "content": "p"}

    def test_complete_handles_null_content(self, openai_env: None) -> None:
        """A ``None`` message content normalizes to empty text, not a crash."""
        client = _make_mock_openai_client(None)  # type: ignore[arg-type]
        with patch("openai.OpenAI", return_value=client):
            provider = OpenAIProvider(LLMConfig(provider="openai"))
            result = provider.complete("p")
        assert result.text == ""

    def test_complete_redacts_sdk_errors(self, openai_env: None) -> None:
        """SDK errors surface only the exception type name, not request state."""
        import openai

        client = MagicMock()
        client.chat.completions.create.side_effect = openai.OpenAIError(
            "secret request payload and api details"
        )
        with patch("openai.OpenAI", return_value=client):
            provider = OpenAIProvider(LLMConfig(provider="openai"))
            with pytest.raises(RuntimeError) as exc:
                provider.complete("p")
        message = str(exc.value)
        assert "OpenAIError" in message
        assert "secret" not in message

    def test_complete_4xx_surfaces_api_message(self, openai_env: None) -> None:
        """A 4xx surfaces OpenAI's own error message (the actionable cause; #745)."""
        import httpx
        import openai

        response = httpx.Response(
            400, request=httpx.Request("POST", "https://api.openai.com")
        )
        client = MagicMock()
        client.chat.completions.create.side_effect = openai.BadRequestError(
            message="You exceeded your current quota", response=response, body=None
        )
        with patch("openai.OpenAI", return_value=client):
            provider = OpenAIProvider(LLMConfig(provider="openai"))
            with pytest.raises(RuntimeError) as exc:
                provider.complete("p")
        message = str(exc.value)
        assert "exceeded your current quota" in message
        assert "HTTP 400" in message

    def test_complete_5xx_withholds_body_and_suppresses_chain(
        self, openai_env: None
    ) -> None:
        """A 5xx is status-only; its chain is suppressed (no body via `__cause__`)."""
        import httpx
        import openai

        response = httpx.Response(
            503, request=httpx.Request("POST", "https://api.openai.com")
        )
        client = MagicMock()
        client.chat.completions.create.side_effect = openai.InternalServerError(
            message="secret internal detail", response=response, body=None
        )
        with patch("openai.OpenAI", return_value=client):
            provider = OpenAIProvider(LLMConfig(provider="openai"))
            with pytest.raises(RuntimeError) as exc:
                provider.complete("p")
        assert "secret internal detail" not in str(exc.value)
        assert "HTTP 503" in str(exc.value)
        assert exc.value.__cause__ is None

    def test_rate_limit_surfaces_retry_after(self, openai_env: None) -> None:
        """A vendor 429 maps to ProviderRateLimitError carrying Retry-After."""
        import httpx
        import openai

        from creek.classify.llm.providers import ProviderRateLimitError

        response = httpx.Response(
            429,
            headers={"retry-after": "7"},
            request=httpx.Request("POST", "https://api.openai.com"),
        )
        client = MagicMock()
        client.chat.completions.create.side_effect = openai.RateLimitError(
            "secret request payload", response=response, body=None
        )
        with patch("openai.OpenAI", return_value=client):
            provider = OpenAIProvider(LLMConfig(provider="openai"))
            with pytest.raises(ProviderRateLimitError) as exc:
                provider.complete("p")
        assert exc.value.retry_after == 7.0
        assert "secret" not in str(exc.value)

    def test_rate_limit_without_header_has_no_retry_after(
        self, openai_env: None
    ) -> None:
        """A 429 without Retry-After still maps, with ``retry_after=None``."""
        import httpx
        import openai

        from creek.classify.llm.providers import ProviderRateLimitError

        response = httpx.Response(
            429, request=httpx.Request("POST", "https://api.openai.com")
        )
        client = MagicMock()
        client.chat.completions.create.side_effect = openai.RateLimitError(
            "m", response=response, body=None
        )
        with patch("openai.OpenAI", return_value=client):
            provider = OpenAIProvider(LLMConfig(provider="openai"))
            with pytest.raises(ProviderRateLimitError) as exc:
                provider.complete("p")
        assert exc.value.retry_after is None

    def test_complete_uses_api_base_when_set(self, openai_env: None) -> None:
        """``config.api_base`` is passed to the SDK client as ``base_url``."""
        client = _make_mock_openai_client("x")
        with patch("openai.OpenAI", return_value=client) as mock_ctor:
            provider = OpenAIProvider(
                LLMConfig(provider="openai", api_base="https://gw.example/v1")
            )
            provider.complete("p")
        assert mock_ctor.call_args.kwargs.get("base_url") == "https://gw.example/v1"


def test_openai_end_to_end_classify(
    openai_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A mocked end-to-end classify via provider=openai returns a Completion."""
    from creek.classify.llm.orchestrator import LLMClassifier

    client = _make_mock_openai_client("response body")
    with patch("openai.OpenAI", return_value=client):
        classifier = LLMClassifier(config=LLMConfig(provider="openai"))
        completion = classifier.invoke_prompt_with_metadata("classify this")
    assert isinstance(completion, Completion)
    assert completion.text == "response body"
    assert completion.usage == {"input_tokens": 11, "output_tokens": 7}
