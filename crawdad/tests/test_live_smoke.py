"""Opt-in live smoke tests: one tiny real API call per async LLM provider.

CrawDad's provider tests mock every vendor SDK, so a model tier is unverified
until the first live run. Each test here proves the full round-trip
(auth → request → normalized ``Completion``) against the live API using the
composer tier resolved by :func:`crawdad.config.composer_model_for` — which
honors the ``CRAWDAD_COMPOSER_MODEL`` env override, so onboarding a new model
is one command::

    ./scripts/test.sh -m integration --no-cov -k openai
    CRAWDAD_COMPOSER_MODEL=new-model ./scripts/test.sh -m integration --no-cov

These tests are ``integration``-marked (deselected by the default
``./scripts/test.sh`` run) and each skips cleanly when its provider's API key
is absent, so normal CI never runs or bills them.
"""

from __future__ import annotations

import os

import pytest

from crawdad.config import composer_model_for
from crawdad.llm import (
    AnthropicAsyncProvider,
    Completion,
    GeminiAsyncProvider,
    OpenAIAsyncProvider,
)

pytestmark = pytest.mark.integration

_PROMPT = "Reply with exactly the word OK and nothing else."


def _messages() -> list[dict[str, str]]:
    """Build the single-turn smoke payload.

    Returns:
        A one-element user message list carrying :data:`_PROMPT`.
    """
    return [{"role": "user", "content": _PROMPT}]


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
async def test_anthropic_live_smoke() -> None:
    """One real Anthropic call normalizes to a populated ``Completion``."""
    import anthropic

    provider = AnthropicAsyncProvider(anthropic.AsyncAnthropic())
    completion = await provider.complete(
        _messages(), model=composer_model_for("anthropic"), max_tokens=16
    )
    _assert_live_completion(completion)


@pytest.mark.skipif(
    not os.environ.get("OPENAI_API_KEY"), reason="OPENAI_API_KEY not set"
)
async def test_openai_live_smoke() -> None:
    """One real OpenAI call normalizes to a populated ``Completion``."""
    import openai

    provider = OpenAIAsyncProvider(openai.AsyncOpenAI())
    completion = await provider.complete(
        _messages(), model=composer_model_for("openai"), max_tokens=16
    )
    _assert_live_completion(completion)


@pytest.mark.skipif(
    not os.environ.get("GOOGLE_API_KEY"), reason="GOOGLE_API_KEY not set"
)
async def test_gemini_live_smoke() -> None:
    """One real Gemini call normalizes to a populated ``Completion``."""
    from google import genai

    provider = GeminiAsyncProvider(genai.Client())
    completion = await provider.complete(
        _messages(), model=composer_model_for("gemini"), max_tokens=16
    )
    _assert_live_completion(completion)
