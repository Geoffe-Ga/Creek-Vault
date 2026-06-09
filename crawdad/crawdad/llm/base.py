"""The async, provider-neutral contract for CrawDad's LLM backends (#609).

CrawDad stays decoupled from ``creek-tools`` (FEAT-013): this module deliberately
mirrors creek-tools' normalized ``Completion`` shape and provider protocol
*without* importing anything from it. The contract is **async** here (CrawDad is
discord.py + MCP) whereas creek-tools' sibling is synchronous.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True)
class Completion:
    """A model completion paired with its stop reason and token usage.

    Mirrors the creek-tools normalized result so both packages speak the same
    shape across the MCP boundary, without sharing a module.

    Attributes:
        text: The concatenated textual content of the response.
        stop_reason: Why the model stopped. ``"end_turn"`` on a clean
            completion; ``"max_tokens"`` when the ceiling was hit.
        usage: Token-usage counts from the SDK response when available, or
            ``None`` when the SDK omitted usage.
    """

    text: str
    stop_reason: str = "end_turn"
    usage: dict[str, int] | None = None


@runtime_checkable
class AsyncLLMProvider(Protocol):
    """An asynchronous, provider-neutral chat-completion backend.

    Implementations wrap a vendor SDK, hold the request/response shape and the
    error redaction, and turn a messages list into a normalized
    :class:`Completion`. Routing through this protocol lets the router and
    composer stay backend-agnostic (Anthropic today; #610 adds more).
    """

    async def complete(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str,
        max_tokens: int,
    ) -> Completion:
        """Send *messages* and return the normalized :class:`Completion`.

        Args:
            messages: The chat messages payload (``role`` / ``content`` dicts).
            model: The resolved model identifier (never a literal in callers).
            max_tokens: Hard cap on the reply length.

        Returns:
            The backend's :class:`Completion`.

        Raises:
            RuntimeError: On any SDK failure, re-raised with only the original
                exception's type name so request state never leaks.
        """
