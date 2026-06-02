"""Thin, mockable Anthropic client wrapper for the author desk (FEAT-041, #455).

The Writing Desk's only seam onto the network goes through this wrapper, so
unit tests inject a mock provider and never make a live call. The skeleton's
specialists/voice/reflection are pure stubs and do not yet call it; issue #460
wires it into the real Conductor.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from creek.classify.llm.providers import AnthropicProvider

if TYPE_CHECKING:
    from creek.config import LLMConfig


class AuthorLLMClient:
    """A thin wrapper over :class:`AnthropicProvider` for author-desk calls.

    Attributes:
        _provider: The underlying provider performing the SDK call.
    """

    def __init__(self, provider: AnthropicProvider) -> None:
        """Store the provider this client delegates to.

        Args:
            provider: The Anthropic provider performing the actual call.
        """
        self._provider = provider

    @classmethod
    def from_config(cls, config: LLMConfig) -> AuthorLLMClient:
        """Build a client from *config*, sourcing the model id from config.

        Args:
            config: The LLM configuration (provider + model id come from here,
                never hard-coded).

        Returns:
            An :class:`AuthorLLMClient` wrapping a configured provider.
        """
        return cls(AnthropicProvider(config))

    def complete(self, prompt: str, *, max_tokens: int | None = None) -> str:
        """Return the completion text for *prompt*.

        Args:
            prompt: The prompt to send.
            max_tokens: Optional output-token ceiling.

        Returns:
            The provider's completion text.
        """
        return self._provider.call_with_metadata(prompt, max_tokens=max_tokens).text
