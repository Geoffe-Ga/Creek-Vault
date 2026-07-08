"""Tests for the provider-neutral seam (#604).

Locks the refactor that extracted a provider-neutral :class:`Completion`
result type and an :class:`LLMProvider` ``Protocol`` the existing
Anthropic path satisfies, without changing any behaviour. Covers:

- the ``AnthropicCompletion`` deprecated alias still resolves to ``Completion``;
- every historical import path (``providers`` and the package shim) still works;
- :class:`AnthropicProvider` is a structural :class:`LLMProvider`;
- the new :meth:`AnthropicProvider.complete` delegates to the historical
  :meth:`AnthropicProvider.call_with_metadata` with no behaviour change;
- :attr:`AnthropicProvider.available` reflects the env prerequisites.
"""

from __future__ import annotations

import pytest

from creek.classify.llm.base import LLMProvider
from creek.classify.llm.completion import AnthropicCompletion, Completion
from creek.classify.llm.providers import AnthropicProvider
from creek.config import LLMConfig


@pytest.fixture
def anthropic_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set the API key and consent so an ``AnthropicProvider`` constructs."""
    monkeypatch.setenv(AnthropicProvider.API_KEY_ENV, "sk-test-not-real")
    monkeypatch.setenv(AnthropicProvider.CONSENT_ENV, "1")


def test_anthropic_completion_is_completion_alias() -> None:
    """The deprecated ``AnthropicCompletion`` name is exactly ``Completion``."""
    assert AnthropicCompletion is Completion


def test_completion_keeps_normalized_fields() -> None:
    """``Completion`` carries the historical text/stop_reason/usage shape."""
    completion = Completion(
        text="hi", stop_reason="max_tokens", usage={"input_tokens": 3}
    )
    assert completion.text == "hi"
    assert completion.stop_reason == "max_tokens"
    assert completion.usage == {"input_tokens": 3}


def test_completion_defaults() -> None:
    """``stop_reason`` defaults to ``end_turn`` and ``usage`` to ``None``."""
    completion = Completion(text="x")
    assert completion.stop_reason == "end_turn"
    assert completion.usage is None


def test_legacy_import_paths_still_resolve() -> None:
    """Every pre-refactor import site keeps working (public surface intact)."""
    from creek.classify.llm import (
        AnthropicCompletion as PkgAnthropicCompletion,
    )
    from creek.classify.llm import (
        Completion as PkgCompletion,
    )
    from creek.classify.llm import (
        LLMProvider as PkgLLMProvider,
    )
    from creek.classify.llm.providers import (
        AnthropicCompletion as ProvidersAnthropicCompletion,
    )

    assert PkgCompletion is Completion
    assert PkgAnthropicCompletion is Completion
    assert ProvidersAnthropicCompletion is Completion
    assert PkgLLMProvider is LLMProvider


def test_anthropic_provider_is_structural_llm_provider(
    anthropic_env: None,
) -> None:
    """A constructed ``AnthropicProvider`` satisfies the ``LLMProvider`` protocol."""
    provider = AnthropicProvider(LLMConfig(provider="anthropic"))
    assert isinstance(provider, LLMProvider)


def test_complete_delegates_to_call_with_metadata(
    anthropic_env: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``complete`` forwards prompt and kwargs verbatim to ``call_with_metadata``."""
    provider = AnthropicProvider(LLMConfig(provider="anthropic"))
    captured: dict[str, object] = {}
    sentinel = Completion(text="ok", stop_reason="end_turn")

    def fake(
        prompt: str,
        *,
        max_tokens: int | None = None,
        system: str | None = None,
    ) -> Completion:
        captured["prompt"] = prompt
        captured["max_tokens"] = max_tokens
        captured["system"] = system
        return sentinel

    monkeypatch.setattr(provider, "call_with_metadata", fake)

    result = provider.complete("hello", max_tokens=42, system="sys")

    assert result is sentinel
    assert captured == {"prompt": "hello", "max_tokens": 42, "system": "sys"}


def test_complete_returns_completion(
    anthropic_env: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``complete`` returns the provider-neutral ``Completion`` type."""
    provider = AnthropicProvider(LLMConfig(provider="anthropic"))
    monkeypatch.setattr(
        provider, "call_with_metadata", lambda *a, **k: Completion(text="z")
    )
    assert isinstance(provider.complete("p"), Completion)


def test_available_true_when_prerequisites_met(anthropic_env: None) -> None:
    """``available`` is ``True`` while key and consent are present."""
    provider = AnthropicProvider(LLMConfig(provider="anthropic"))
    assert provider.available is True


def test_available_false_when_key_removed(
    anthropic_env: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``available`` re-checks env and flips ``False`` once the key is gone."""
    provider = AnthropicProvider(LLMConfig(provider="anthropic"))
    monkeypatch.delenv(AnthropicProvider.API_KEY_ENV, raising=False)
    assert provider.available is False


def test_available_false_when_consent_revoked(
    anthropic_env: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``available`` flips ``False`` when consent is no longer affirmative."""
    provider = AnthropicProvider(LLMConfig(provider="anthropic"))
    monkeypatch.setenv(AnthropicProvider.CONSENT_ENV, "no")
    assert provider.available is False
