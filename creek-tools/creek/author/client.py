"""Thin, mockable Anthropic client wrapper for the author desk (FEAT-041, #455).

The Writing Desk's only seam onto the network goes through this wrapper, so
unit tests inject a mock provider and never make a live call. The skeleton's
specialists/voice/reflection are pure stubs and do not yet call it; issue #460
wires it into the real Conductor.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from creek.classify.llm.providers import AnthropicCompletion, AnthropicProvider

if TYPE_CHECKING:
    from creek.config import AuthorConfig, LLMConfig


def resolve_voice_model(author: AuthorConfig, llm: LLMConfig) -> str:
    """Resolve the model id for the voice call from config, never hard-coded.

    The desk's per-agent model tiers (#474) let an operator point the voice
    call at a different model than the rest of the pipeline. The voice tier
    falls back to the shared ``llm`` model when unset, so no model id is
    literal in :mod:`creek.author`.

    Args:
        author: The author-subsystem config carrying the per-agent overrides.
        llm: The shared LLM config supplying the fallback model id.

    Returns:
        :attr:`AuthorConfig.voice_model` when set, otherwise ``llm.model``.
    """
    return author.voice_model or llm.model


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
    def from_config(
        cls, config: LLMConfig, *, model: str | None = None
    ) -> AuthorLLMClient:
        """Build a client from *config*, sourcing the model id from config.

        Args:
            config: The LLM configuration (provider + model id come from here,
                never hard-coded).
            model: Optional per-agent model override (e.g. the resolved voice
                tier). When supplied it replaces ``config.model`` so the desk
                can run a different model per agent without any literal id in
                :mod:`creek.author`. ``None`` keeps ``config.model`` unchanged.

        Returns:
            An :class:`AuthorLLMClient` wrapping a configured provider.
        """
        effective = (
            config if model is None else config.model_copy(update={"model": model})
        )
        return cls(AnthropicProvider(effective))

    def complete(self, prompt: str, *, max_tokens: int | None = None) -> str:
        """Return the completion text for *prompt*.

        Args:
            prompt: The prompt to send.
            max_tokens: Optional output-token ceiling.

        Returns:
            The provider's completion text.
        """
        return self._provider.call_with_metadata(prompt, max_tokens=max_tokens).text

    def complete_with_usage(
        self,
        prompt: str,
        *,
        system: str | None = None,
        cache_control: dict[str, str] | None = None,
        max_tokens: int | None = None,
    ) -> AnthropicCompletion:
        """Return the full completion (text + token usage) for *prompt*.

        Unlike :meth:`complete` (which yields only the text and is kept stable
        for existing callers), this exposes the SDK's token usage so the desk
        can surface a run's cost and cache-hit rate. A static *system* prefix is
        sent as a cached block (#474).

        Args:
            prompt: The dynamic user prompt.
            system: Optional static system prefix to cache.
            cache_control: Cache directive for the system block; defaults to
                ephemeral inside the provider when *system* is set.
            max_tokens: Optional output-token ceiling.

        Returns:
            The provider's :class:`AnthropicCompletion`, carrying text and
            :attr:`~AnthropicCompletion.usage`.
        """
        return self._provider.call_with_metadata(
            prompt,
            system=system,
            cache_control=cache_control,
            max_tokens=max_tokens,
        )
