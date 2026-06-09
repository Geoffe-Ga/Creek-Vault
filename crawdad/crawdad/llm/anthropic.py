"""The Anthropic async provider for CrawDad (#609).

Wraps ``anthropic.AsyncAnthropic`` behind the :class:`AsyncLLMProvider`
protocol, owning the ``messages.create`` call shape and the
``AnthropicError`` → ``RuntimeError(type name)`` redaction that previously
lived inline in the router and composer. No model identifier is referenced
here — callers pass the resolved model (see ``tests/test_no_model_literals.py``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

import anthropic

from crawdad.llm.base import Completion

if TYPE_CHECKING:
    from anthropic.types import MessageParam


class AnthropicAsyncProvider:
    """An :class:`~crawdad.llm.base.AsyncLLMProvider` backed by Anthropic.

    Attributes:
        _client: The injected ``anthropic.AsyncAnthropic`` client (or a test
            double with the same ``messages.create`` shape).
    """

    def __init__(self, client: anthropic.AsyncAnthropic) -> None:
        """Store the async Anthropic client this provider delegates to.

        Args:
            client: An ``anthropic.AsyncAnthropic`` instance.
        """
        self._client = client

    async def complete(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str,
        max_tokens: int,
    ) -> Completion:
        """Send *messages* to Anthropic and return a normalized completion.

        Args:
            messages: The chat messages payload.
            model: The resolved model identifier.
            max_tokens: Hard cap on the reply length.

        Returns:
            A :class:`Completion` wrapping the concatenated response text and
            the reported stop reason.

        Raises:
            RuntimeError: If the SDK raises any ``AnthropicError``; re-raised
                with only the exception type name so no request state leaks.
        """
        try:
            response = await self._client.messages.create(
                model=model,
                max_tokens=max_tokens,
                # Bridge the provider-neutral messages shape to the SDK's typed
                # param at the vendor boundary; callers build role/content dicts.
                messages=cast("list[MessageParam]", messages),
            )
        except anthropic.AnthropicError as exc:
            raise RuntimeError(type(exc).__name__) from None
        stop_reason = getattr(response, "stop_reason", None) or "end_turn"
        return Completion(
            text=_extract_text(response),
            stop_reason=str(stop_reason),
        )


def _extract_text(response: Any) -> str:
    """Pull the concatenated text from a (possibly multi-block) reply.

    Args:
        response: A ``messages.create`` response object.

    Returns:
        The joined ``text`` of each content block.
    """
    parts: list[str] = []
    for block in getattr(response, "content", []) or []:
        text = getattr(block, "text", None)
        if text is not None:
            parts.append(str(text))
    return "\n".join(parts)
