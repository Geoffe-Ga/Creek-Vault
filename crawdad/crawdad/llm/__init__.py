"""CrawDad's async LLM provider seam (#609, #610).

Exposes the provider-neutral :class:`AsyncLLMProvider` protocol and
:class:`Completion` shape, the Anthropic / OpenAI / Gemini implementations, and
the :func:`build_async_provider` factory the CLI uses to wire the router and
composer to the backend selected by ``CRAWDAD_PROVIDER``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from crawdad.llm.anthropic import AnthropicAsyncProvider
from crawdad.llm.base import AsyncLLMProvider, Completion
from crawdad.llm.gemini import GeminiAsyncProvider
from crawdad.llm.openai import OpenAIAsyncProvider

if TYPE_CHECKING:
    from crawdad.config import CrawDadConfig

__all__ = [
    "AnthropicAsyncProvider",
    "AsyncLLMProvider",
    "Completion",
    "GeminiAsyncProvider",
    "OpenAIAsyncProvider",
    "build_async_provider",
]


def _build_anthropic() -> AsyncLLMProvider:
    """Build the Anthropic provider; the SDK reads ``ANTHROPIC_API_KEY``."""
    import anthropic

    return AnthropicAsyncProvider(anthropic.AsyncAnthropic())


def _build_openai() -> AsyncLLMProvider:
    """Build the OpenAI provider; the SDK reads ``OPENAI_API_KEY``."""
    import openai

    return OpenAIAsyncProvider(openai.AsyncOpenAI())


def _build_gemini() -> AsyncLLMProvider:
    """Build the Gemini provider; the SDK reads ``GOOGLE_API_KEY``."""
    from google import genai

    return GeminiAsyncProvider(genai.Client())


#: Provider name → zero-arg builder. Each builder constructs its SDK client,
#: which reads the API key from the environment (never a stored secret).
_PROVIDER_BUILDERS = {
    "anthropic": _build_anthropic,
    "openai": _build_openai,
    "gemini": _build_gemini,
}


def build_async_provider(config: CrawDadConfig) -> AsyncLLMProvider:
    """Construct the async LLM provider selected by ``config.llm_provider``.

    Each SDK client reads its API key from the environment, so no secret is
    passed through or stored here.

    Args:
        config: The loaded CrawDad configuration.

    Returns:
        An :class:`AsyncLLMProvider` ready to back the router and composer.

    Raises:
        ValueError: If ``config.llm_provider`` names no registered backend.
    """
    builder = _PROVIDER_BUILDERS.get(config.llm_provider)
    if builder is None:
        known = ", ".join(sorted(_PROVIDER_BUILDERS))
        msg = f"unknown provider {config.llm_provider!r}; expected one of: {known}"
        raise ValueError(msg)
    return builder()
