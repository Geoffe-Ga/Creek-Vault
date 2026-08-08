"""Opt-in live smoke tests: one tiny real API call per LLM provider.

Every other provider test mocks the vendor SDK, which is right for unit
coverage but means a model id is unverified until the first live run — and
the things mocks paper over (valid model ids, stop-reason enums, usage field
names) are exactly what drift. Each test here proves the full round-trip
(auth → request → normalized ``Completion``) against the live API.

These tests are ``live``-marked, so neither the default test selection nor any
CI lane runs (or bills) them — CI holds no provider credentials, and a gate
that depends on a paid third-party API is a flaky gate. Each one also skips
cleanly when its provider's API key is absent. To smoke a provider when
onboarding a new model id::

    ./scripts/test.sh --live -k openai
    CREEK_SMOKE_MODEL=some-new-model ./scripts/test.sh --live -k openai

``CREEK_SMOKE_MODEL`` overrides the provider's default model for the run;
unset, each provider exercises its own ``DEFAULT_MODELS`` entry.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

import pytest

from creek.classify.llm.consent import CLOUD_CONSENT_ENV
from creek.classify.llm.providers import (
    AnthropicProvider,
    GeminiProvider,
    OllamaProvider,
    OpenAIProvider,
)
from creek.config import LLMConfig

if TYPE_CHECKING:
    from creek.classify.llm.completion import Completion

pytestmark = pytest.mark.live

_PROMPT = "Reply with exactly the word OK and nothing else."

_SMOKE_MODEL_ENV = "CREEK_SMOKE_MODEL"
"""Env var overriding the model id under test (unset → provider default)."""


def _smoke_config(provider: str) -> LLMConfig:
    """Build the config for a smoke run, honoring the model override.

    Args:
        provider: The registered provider name to smoke.

    Returns:
        An ``LLMConfig`` whose model is :data:`_SMOKE_MODEL_ENV` when set,
        else ``None`` so the provider applies its own default.
    """
    return LLMConfig(provider=provider, model=os.environ.get(_SMOKE_MODEL_ENV))


def _assert_live_completion(completion: Completion) -> None:
    """Assert the normalized shape every live round-trip must produce.

    Args:
        completion: The provider's normalized result for :data:`_PROMPT`.
    """
    assert completion.text.strip(), "live completion returned empty text"
    assert completion.stop_reason in {"end_turn", "max_tokens"}, (
        f"unrecognized stop reason {completion.stop_reason!r}"
    )


@pytest.mark.skipif(
    not os.environ.get("ANTHROPIC_API_KEY"), reason="ANTHROPIC_API_KEY not set"
)
def test_anthropic_live_smoke(monkeypatch: pytest.MonkeyPatch) -> None:
    """One real Anthropic call normalizes to a populated ``Completion``."""
    monkeypatch.setenv(CLOUD_CONSENT_ENV, "1")
    provider = AnthropicProvider(_smoke_config("anthropic"))
    completion = provider.complete(_PROMPT, max_tokens=16)
    _assert_live_completion(completion)
    assert completion.usage is not None


@pytest.mark.skipif(
    not os.environ.get("OPENAI_API_KEY"), reason="OPENAI_API_KEY not set"
)
def test_openai_live_smoke(monkeypatch: pytest.MonkeyPatch) -> None:
    """One real OpenAI call normalizes to a populated ``Completion``."""
    monkeypatch.setenv(CLOUD_CONSENT_ENV, "1")
    provider = OpenAIProvider(_smoke_config("openai"))
    completion = provider.complete(_PROMPT, max_tokens=16)
    _assert_live_completion(completion)
    assert completion.usage is not None


@pytest.mark.skipif(
    not any(os.environ.get(var) for var in GeminiProvider.API_KEY_ENVS),
    reason="neither GOOGLE_API_KEY nor GEMINI_API_KEY is set",
)
def test_gemini_live_smoke(monkeypatch: pytest.MonkeyPatch) -> None:
    """One real Gemini call normalizes to a populated ``Completion``."""
    monkeypatch.setenv(CLOUD_CONSENT_ENV, "1")
    provider = GeminiProvider(_smoke_config("gemini"))
    completion = provider.complete(_PROMPT, max_tokens=16)
    _assert_live_completion(completion)
    assert completion.usage is not None


def test_ollama_live_smoke() -> None:
    """One real local Ollama call normalizes to a populated ``Completion``.

    Gated on endpoint reachability rather than a key — Ollama is local and
    keyless. Ollama's generate API reports no token usage, so ``usage`` stays
    ``None`` by contract.
    """
    provider = OllamaProvider(_smoke_config("ollama"))
    if not provider.available:
        pytest.skip("Ollama endpoint not reachable")
    completion = provider.complete(_PROMPT, max_tokens=16)
    _assert_live_completion(completion)
    assert completion.usage is None
