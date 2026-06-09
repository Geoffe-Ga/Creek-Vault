"""Provider-neutral completion result for the classification pipeline.

Extracted from :mod:`creek.classify.llm.providers` (#604) so every LLM
backend — Anthropic today, OpenAI / Gemini next — returns one normalized
shape. The fields are byte-for-byte identical to the historical
``AnthropicCompletion``; the old name remains as a deprecated alias so no
import site changes.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Completion:
    """A model completion paired with its stop reason and token usage.

    The classification path discards the stop reason; the draft path
    needs it so a ``max_tokens`` ceiling can be surfaced instead of
    truncating the essay mid-sentence without warning.

    Attributes:
        text: The concatenated textual content of the response.
        stop_reason: The reason the model stopped generating. ``"end_turn"``
            on a clean completion; ``"max_tokens"`` when the ceiling was hit.
        usage: Token-usage counts from the SDK response, when available
            (``input_tokens``, ``output_tokens`` and, when prompt caching is
            active, ``cache_creation_input_tokens`` / ``cache_read_input_tokens``).
            ``None`` when the SDK omitted usage. The author desk surfaces this on
            :class:`~creek.author.models.AuthoredDraft` so a run's cost — and
            cache-hit rate — is observable (#474).
    """

    text: str
    stop_reason: str = "end_turn"
    usage: dict[str, int] | None = None


AnthropicCompletion = Completion
"""Deprecated alias for :class:`Completion`.

Retained so pre-#604 import sites (``from creek.classify.llm.providers import
AnthropicCompletion``) keep resolving. New code should use :class:`Completion`.
"""
