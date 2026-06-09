"""The OpenAI async provider for CrawDad (#610).

Wraps ``openai.AsyncOpenAI`` behind the :class:`AsyncLLMProvider` protocol,
mirroring the Anthropic implementation and the creek-tools sync OpenAI provider
(#607): same ``chat.completions`` shape, ``finish_reason`` / usage mapping, and
``OpenAIError`` → ``RuntimeError(type name)`` redaction. The key is read from
``OPENAI_API_KEY`` by the SDK; no model literal lives here.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

import openai

from crawdad.llm.base import Completion

if TYPE_CHECKING:
    from openai.types.chat import ChatCompletionMessageParam

#: OpenAI ``finish_reason`` → normalized :class:`Completion` stop reason.
_OPENAI_STOP_REASONS: dict[str, str] = {"stop": "end_turn", "length": "max_tokens"}


class OpenAIAsyncProvider:
    """An :class:`~crawdad.llm.base.AsyncLLMProvider` backed by OpenAI.

    Attributes:
        _client: The injected ``openai.AsyncOpenAI`` client (or a test double).
    """

    def __init__(self, client: openai.AsyncOpenAI) -> None:
        """Store the async OpenAI client this provider delegates to.

        Args:
            client: An ``openai.AsyncOpenAI`` instance.
        """
        self._client = client

    async def complete(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str,
        max_tokens: int,
    ) -> Completion:
        """Send *messages* to OpenAI and return a normalized completion.

        Args:
            messages: The chat messages payload.
            model: The resolved model identifier.
            max_tokens: Hard cap on the reply length.

        Returns:
            A :class:`Completion` with the response text, mapped stop reason,
            and token usage when present.

        Raises:
            RuntimeError: If the SDK raises any ``OpenAIError``; re-raised with
                only the exception type name so no request state leaks.
        """
        try:
            response = await self._client.chat.completions.create(
                model=model,
                max_tokens=max_tokens,
                messages=cast("list[ChatCompletionMessageParam]", messages),
            )
        except openai.OpenAIError as exc:
            raise RuntimeError(type(exc).__name__) from None
        return Completion(
            text=_extract_text(response),
            stop_reason=_map_stop_reason(response),
            usage=_extract_usage(response),
        )


def _extract_text(response: Any) -> str:
    """Return the first choice's message content, or ``""`` when absent.

    Args:
        response: A ``chat.completions.create`` response object.

    Returns:
        The message content string, or empty when missing/non-string.
    """
    choices = getattr(response, "choices", None) or []
    if not choices:
        return ""
    content = getattr(getattr(choices[0], "message", None), "content", None)
    return content if isinstance(content, str) else ""


def _map_stop_reason(response: Any) -> str:
    """Map an OpenAI ``finish_reason`` to a normalized stop reason.

    Args:
        response: A ``chat.completions.create`` response object.

    Returns:
        ``"end_turn"`` for ``"stop"`` (and unknown/absent), ``"max_tokens"``
        for ``"length"``.
    """
    choices = getattr(response, "choices", None) or []
    if not choices:
        return "end_turn"
    reason = getattr(choices[0], "finish_reason", None)
    if not isinstance(reason, str):
        return "end_turn"
    return _OPENAI_STOP_REASONS.get(reason, "end_turn")


def _extract_usage(response: Any) -> dict[str, int] | None:
    """Extract integer token-usage counts from an OpenAI chat response.

    Args:
        response: A ``chat.completions.create`` response object.

    Returns:
        A dict with ``input_tokens`` / ``output_tokens``, or ``None`` when the
        SDK omitted usage.
    """
    usage = getattr(response, "usage", None)
    if usage is None:
        return None
    counts: dict[str, int] = {}
    prompt_tokens = getattr(usage, "prompt_tokens", None)
    completion_tokens = getattr(usage, "completion_tokens", None)
    if isinstance(prompt_tokens, int):
        counts["input_tokens"] = prompt_tokens
    if isinstance(completion_tokens, int):
        counts["output_tokens"] = completion_tokens
    return counts or None
