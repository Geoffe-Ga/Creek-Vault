"""The Google Gemini async provider for CrawDad (#610).

Wraps the ``google-genai`` async client (``genai.Client().aio``) behind the
:class:`AsyncLLMProvider` protocol, mirroring the creek-tools sync Gemini
provider (#608): same ``generate_content`` shape, ``finish_reason`` / usage
mapping, and ``APIError`` → ``RuntimeError(type name)`` redaction. The key is
read from ``GOOGLE_API_KEY`` / ``GEMINI_API_KEY`` by the SDK; no model literal
lives here.
"""

from __future__ import annotations

from typing import Any

from crawdad.llm.base import Completion

#: Gemini ``finish_reason`` → normalized :class:`Completion` stop reason.
_GEMINI_STOP_REASONS: dict[str, str] = {"STOP": "end_turn", "MAX_TOKENS": "max_tokens"}


class GeminiAsyncProvider:
    """An :class:`~crawdad.llm.base.AsyncLLMProvider` backed by Gemini.

    Attributes:
        _client: The injected ``google.genai.Client`` (or a test double); its
            ``.aio`` accessor drives the async ``generate_content`` call.
    """

    def __init__(self, client: Any) -> None:
        """Store the genai client this provider delegates to.

        Args:
            client: A ``google.genai.Client`` instance.
        """
        self._client = client

    async def complete(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str,
        max_tokens: int,
    ) -> Completion:
        """Send *messages* to Gemini and return a normalized completion.

        Args:
            messages: The chat messages payload; their contents are joined into
                a single ``contents`` string (CrawDad sends one user turn).
            model: The resolved model identifier.
            max_tokens: Hard cap on the reply length.

        Returns:
            A :class:`Completion` with the response text, mapped stop reason,
            and token usage when present.

        Raises:
            RuntimeError: If the SDK raises any ``APIError``; re-raised with
                only the exception type name so no request state leaks.
        """
        from google.genai import errors, types

        config = types.GenerateContentConfig(max_output_tokens=max_tokens)
        try:
            response = await self._client.aio.models.generate_content(
                model=model,
                contents=_messages_to_contents(messages),
                config=config,
            )
        except errors.APIError as exc:
            raise RuntimeError(type(exc).__name__) from None
        return Completion(
            text=_extract_text(response),
            stop_reason=_map_stop_reason(response),
            usage=_extract_usage(response),
        )


def _messages_to_contents(messages: list[dict[str, Any]]) -> str:
    """Join message contents into a single prompt string for Gemini.

    Args:
        messages: The chat messages payload.

    Returns:
        The newline-joined ``content`` of each message.
    """
    parts = [str(m.get("content", "")) for m in messages]
    return "\n".join(part for part in parts if part)


def _extract_text(response: Any) -> str:
    """Concatenate the textual parts of the first candidate.

    Args:
        response: A ``generate_content`` response object.

    Returns:
        The joined ``text`` of each content part, or ``""`` when absent.
    """
    candidates = getattr(response, "candidates", None) or []
    if not candidates:
        return ""
    content = getattr(candidates[0], "content", None)
    parts = getattr(content, "parts", None) or []
    out: list[str] = []
    for part in parts:
        text = getattr(part, "text", None)
        if isinstance(text, str):
            out.append(text)
    return "".join(out)


def _map_stop_reason(response: Any) -> str:
    """Map a Gemini ``finish_reason`` (enum or string) to a stop reason.

    Args:
        response: A ``generate_content`` response object.

    Returns:
        ``"end_turn"`` for ``STOP`` (and unknown/absent), ``"max_tokens"`` for
        ``MAX_TOKENS``.
    """
    candidates = getattr(response, "candidates", None) or []
    if not candidates:
        return "end_turn"
    reason = getattr(candidates[0], "finish_reason", None)
    if reason is None:
        return "end_turn"
    name = getattr(reason, "name", None)
    key = name if isinstance(name, str) else str(reason)
    return _GEMINI_STOP_REASONS.get(key, "end_turn")


def _extract_usage(response: Any) -> dict[str, int] | None:
    """Extract integer token-usage counts from a Gemini response.

    Args:
        response: A ``generate_content`` response object.

    Returns:
        A dict with ``input_tokens`` / ``output_tokens``, or ``None`` when the
        SDK omitted usage.
    """
    usage = getattr(response, "usage_metadata", None)
    if usage is None:
        return None
    counts: dict[str, int] = {}
    prompt_tokens = getattr(usage, "prompt_token_count", None)
    candidate_tokens = getattr(usage, "candidates_token_count", None)
    if isinstance(prompt_tokens, int):
        counts["input_tokens"] = prompt_tokens
    if isinstance(candidate_tokens, int):
        counts["output_tokens"] = candidate_tokens
    return counts or None
