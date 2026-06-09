"""CrawDad's async LLM provider seam (#609).

Exposes the provider-neutral :class:`AsyncLLMProvider` protocol and
:class:`Completion` shape, the Anthropic implementation, and the
:func:`build_async_provider` factory the CLI uses to wire the router and
composer to a backend. Anthropic is the only provider for now; #610 registers
OpenAI and Gemini here.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from crawdad.llm.anthropic import AnthropicAsyncProvider
from crawdad.llm.base import AsyncLLMProvider, Completion

if TYPE_CHECKING:
    from crawdad.config import CrawDadConfig

__all__ = [
    "AnthropicAsyncProvider",
    "AsyncLLMProvider",
    "Completion",
    "build_async_provider",
]


def build_async_provider(config: CrawDadConfig) -> AsyncLLMProvider:
    """Construct the async LLM provider selected by *config*.

    The single place CrawDad chooses a backend; Anthropic only for now. The
    API key is read from ``config.anthropic_api_key`` and handed to the SDK,
    never logged.

    Args:
        config: The loaded CrawDad configuration.

    Returns:
        An :class:`AsyncLLMProvider` ready to back the router and composer.
    """
    import anthropic

    client = anthropic.AsyncAnthropic(api_key=config.anthropic_api_key)
    return AnthropicAsyncProvider(client)
