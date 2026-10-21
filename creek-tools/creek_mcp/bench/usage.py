"""The provider's own token report, and the bound a run without one may claim.

Every cost figure in ADR-0016 rested on a planning assumption — 2,500 prompt
tokens and 128 output tokens per reflection — because nothing in the harness
could read a real count. This module is the seam that replaces it: a completion
callable may report what *it* tokenized, and the harness records that number
rather than deriving one.

Two rules keep the figures honest.

**Nothing is guessed.** A backend with no usage counters leaves both counts
``None`` and the run says :attr:`TokenBasis.UNREPORTED`. A ``None`` is a
statement that the count is unknown; a zero would be a claim about it.

**The basis travels with the number.** A count from a real runtime's counters
(:attr:`TokenBasis.PROVIDER_REPORTED`) is citable in a cost line.
:func:`byte_upper_bound_usage` is not: it is what ``--mode fake`` can honestly
report about itself, one token per UTF-8 byte, which no tokenizer can exceed.
It bounds a figure; it does not estimate one.

Counts only. A :class:`TokenUsage` has no field a prompt or a completion could
hide in, which is what lets the content-free report carry it.
"""

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, runtime_checkable


class TokenBasis(StrEnum):
    """Where a run's token counts came from, and so what may cite them.

    Attributes:
        PROVIDER_REPORTED: The runtime's own usage counters (Ollama's
            ``prompt_eval_count`` / ``eval_count``). Citable.
        BYTE_UPPER_BOUND: One token per UTF-8 byte, self-reported by a model
            with no tokenizer. An upper bound, never an estimate.
        UNREPORTED: The pass ran and the backend reported no counts.
        NOT_MEASURED: The per-surface pass was not run at all.
    """

    PROVIDER_REPORTED = "provider_reported"
    BYTE_UPPER_BOUND = "byte_upper_bound"
    UNREPORTED = "unreported"
    NOT_MEASURED = "not_measured"


@dataclass(frozen=True, slots=True)
class TokenUsage:
    """One completion turn's token counts, as its provider reported them.

    Attributes:
        prompt_tokens: Tokens the provider evaluated for the prompt, or
            ``None`` when it reported none.
        output_tokens: Tokens it generated, or ``None`` when it reported none.
        basis: Where the counts came from.
    """

    prompt_tokens: int | None
    output_tokens: int | None
    basis: TokenBasis


@runtime_checkable
class UsageReporting(Protocol):
    """A completion callable that can report its own last turn's usage.

    An optional sibling of :class:`~creek_mcp.bench.protocols.LLMCallable`:
    the harness probes for it structurally and asks nothing of a backend that
    does not implement it. :meth:`token_usage` reads counts already parsed, so
    it must not raise and must not perform I/O — a trial is already decided by
    the time it is called.
    """

    def token_usage(self) -> TokenUsage | None:
        """Return the last completion's usage, or ``None`` if unreported."""


def byte_upper_bound_usage(prompt: str, completion: str) -> TokenUsage:
    """Return the byte-level upper bound on *prompt* and *completion*.

    A byte-level tokenizer never spends less than one byte per token, so the
    UTF-8 length of a string is an upper bound on its token count. That makes
    this safe to compare against a ceiling and wrong to quote as a cost.

    Args:
        prompt: The prompt the model was given.
        completion: The text it returned.

    Returns:
        The bound, labelled :attr:`TokenBasis.BYTE_UPPER_BOUND`.
    """
    return TokenUsage(
        prompt_tokens=len(prompt.encode("utf-8")),
        output_tokens=len(completion.encode("utf-8")),
        basis=TokenBasis.BYTE_UPPER_BOUND,
    )
