"""Tests for the provider factory + OllamaProvider seam (#605).

Locks the refactor that collapsed the scattered ``provider == "anthropic"``
branching into one :func:`build_provider` registry and routed both consumers
(classifier + author desk) through the :class:`LLMProvider` Protocol. Covers:

- :func:`build_provider` returns the right class per provider string and
  raises a clear ``ValueError`` for an unknown provider;
- :class:`OllamaProvider` implements the protocol (``model`` / ``available`` /
  ``complete`` / ``is_cloud``) and wraps the existing Ollama HTTP helpers;
- :func:`provider_is_cloud` reports cloud egress *without* instantiating
  (so the classifier can warn at construction even with no API key set);
- the Anthropic path keeps ``is_cloud=True``.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from creek.classify.llm.base import LLMProvider
from creek.classify.llm.completion import Completion
from creek.classify.llm.providers import (
    AnthropicProvider,
    OllamaProvider,
    build_provider,
    provider_is_cloud,
)
from creek.config import LLMConfig


@pytest.fixture
def anthropic_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set the API key and consent so an ``AnthropicProvider`` constructs."""
    monkeypatch.setenv(AnthropicProvider.API_KEY_ENV, "sk-test-not-real")
    monkeypatch.setenv(AnthropicProvider.CONSENT_ENV, "1")


def test_build_provider_returns_ollama_by_default() -> None:
    """The default ``ollama`` provider builds an ``OllamaProvider``."""
    provider = build_provider(LLMConfig())
    assert isinstance(provider, OllamaProvider)
    assert isinstance(provider, LLMProvider)


def test_build_provider_returns_anthropic(anthropic_env: None) -> None:
    """``provider="anthropic"`` builds an ``AnthropicProvider``."""
    provider = build_provider(LLMConfig(provider="anthropic"))
    assert isinstance(provider, AnthropicProvider)
    assert isinstance(provider, LLMProvider)


def test_build_provider_unknown_raises_valueerror() -> None:
    """An unknown provider string raises a clear ``ValueError``."""
    with pytest.raises(ValueError, match="unknown LLM provider 'frobnicate'"):
        build_provider(LLMConfig(provider="frobnicate"))


def test_build_provider_valueerror_lists_known_providers() -> None:
    """The error names the supported providers so the operator can self-serve."""
    with pytest.raises(ValueError, match=r"anthropic.*ollama|ollama.*anthropic"):
        build_provider(LLMConfig(provider="nope"))


def test_provider_is_cloud_anthropic_true() -> None:
    """Anthropic is a cloud backend (drives the egress warning)."""
    assert provider_is_cloud("anthropic") is True


def test_provider_is_cloud_ollama_false() -> None:
    """Ollama is local — never a cloud backend."""
    assert provider_is_cloud("ollama") is False


def test_provider_is_cloud_unknown_false() -> None:
    """An unknown provider is treated as non-cloud (no spurious warning)."""
    assert provider_is_cloud("frobnicate") is False


def test_provider_is_cloud_does_not_instantiate() -> None:
    """``provider_is_cloud`` must not construct the provider (no env needed).

    The classifier calls this at construction time, before any API key is
    guaranteed present; instantiating Anthropic here would raise.
    """
    # No anthropic_env fixture: if this constructed AnthropicProvider it would
    # raise RuntimeError. It must simply report the class-level flag.
    assert provider_is_cloud("anthropic") is True


def test_anthropic_provider_is_cloud(anthropic_env: None) -> None:
    """``AnthropicProvider`` advertises cloud egress via ``is_cloud``."""
    assert AnthropicProvider(LLMConfig(provider="anthropic")).is_cloud is True


class TestOllamaProvider:
    """Tests for the OllamaProvider wrapper."""

    def test_is_cloud_false(self) -> None:
        """Ollama runs locally; ``is_cloud`` is ``False``."""
        assert OllamaProvider(LLMConfig()).is_cloud is False

    def test_model_returns_config_model(self) -> None:
        """``model`` is the configured Ollama model verbatim."""
        assert OllamaProvider(LLMConfig(model="llama3")).model == "llama3"

    @patch("creek.classify.llm.httpx.Client")
    def test_available_true_on_200(self, mock_client_cls: MagicMock) -> None:
        """``available`` is ``True`` when the Ollama health check returns 200."""
        mock_resp = MagicMock(status_code=200)
        ctx = MagicMock()
        ctx.get.return_value = mock_resp
        mock_client_cls.return_value.__enter__ = MagicMock(return_value=ctx)
        mock_client_cls.return_value.__exit__ = MagicMock(return_value=False)
        assert OllamaProvider(LLMConfig()).available is True

    @patch("creek.classify.llm.httpx.Client")
    def test_available_false_on_connect_error(self, mock_client_cls: MagicMock) -> None:
        """``available`` is ``False`` when the health check connection fails."""
        import httpx

        ctx = MagicMock()
        ctx.get.side_effect = httpx.ConnectError("refused")
        mock_client_cls.return_value.__enter__ = MagicMock(return_value=ctx)
        mock_client_cls.return_value.__exit__ = MagicMock(return_value=False)
        assert OllamaProvider(LLMConfig()).available is False

    @patch("creek.classify.llm.httpx.Client")
    def test_complete_returns_completion_with_response_text(
        self, mock_client_cls: MagicMock
    ) -> None:
        """``complete`` wraps the Ollama response text in a ``Completion``."""
        mock_resp = MagicMock(status_code=200)
        mock_resp.json.return_value = {"response": "hello from ollama"}
        ctx = MagicMock()
        ctx.post.return_value = mock_resp
        mock_client_cls.return_value.__enter__ = MagicMock(return_value=ctx)
        mock_client_cls.return_value.__exit__ = MagicMock(return_value=False)

        result = OllamaProvider(LLMConfig()).complete("prompt")

        assert isinstance(result, Completion)
        assert result.text == "hello from ollama"
        assert result.stop_reason == "end_turn"
        assert result.usage is None

    @patch("creek.classify.llm.httpx.Client")
    def test_complete_ignores_max_tokens_and_system(
        self, mock_client_cls: MagicMock
    ) -> None:
        """Ollama has no max_tokens/system knobs; extra kwargs are accepted."""
        mock_resp = MagicMock(status_code=200)
        mock_resp.json.return_value = {"response": "ok"}
        ctx = MagicMock()
        ctx.post.return_value = mock_resp
        mock_client_cls.return_value.__enter__ = MagicMock(return_value=ctx)
        mock_client_cls.return_value.__exit__ = MagicMock(return_value=False)

        result = OllamaProvider(LLMConfig()).complete("p", max_tokens=99, system="sys")
        assert result.text == "ok"


class _FakeProvider:
    """Minimal LLMProvider stand-in capturing complete() calls."""

    is_cloud = False

    def __init__(self) -> None:
        """Record constructed state for assertions."""
        self.calls: list[tuple[str, int | None, str | None]] = []
        self.available = True
        self.model = "fake"

    def complete(
        self,
        prompt: str,
        *,
        max_tokens: int | None = None,
        system: str | None = None,
    ) -> Completion:
        """Capture the call and return a canned completion."""
        self.calls.append((prompt, max_tokens, system))
        return Completion(text="from-fake", stop_reason="end_turn")


def test_classifier_routes_invoke_through_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``LLMClassifier`` dispatches prompts through the built provider."""
    from creek.classify.llm.orchestrator import LLMClassifier

    fake = _FakeProvider()
    monkeypatch.setattr(
        "creek.classify.llm.orchestrator.build_provider", lambda _config: fake
    )
    classifier = LLMClassifier(config=LLMConfig())

    assert classifier.invoke_prompt("hi") == "from-fake"
    meta = classifier.invoke_prompt_with_metadata("yo", max_tokens=7)
    assert meta.text == "from-fake"
    assert fake.calls == [("hi", None, None), ("yo", 7, None)]


def test_classifier_available_delegates_to_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``available`` reflects the built provider's own availability."""
    from creek.classify.llm.orchestrator import LLMClassifier

    fake = _FakeProvider()
    fake.available = False
    monkeypatch.setattr(
        "creek.classify.llm.orchestrator.build_provider", lambda _config: fake
    )
    assert LLMClassifier(config=LLMConfig()).available is False
