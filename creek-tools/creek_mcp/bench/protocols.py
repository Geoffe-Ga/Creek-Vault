"""Structural types for the model seam the harness drives.

These mirror the private ``_LLM`` / ``_LLMFactory`` protocols in
:mod:`creek_mcp.tools.reflect` structurally rather than importing them, so the
harness depends on the *shape* ``reflect_tool`` accepts and not on that
module's private names. Any object satisfying these satisfies reflect's.
Annotations are evaluated eagerly (no postponed annotations), so every name
they use is a real import.
"""

from pathlib import Path
from typing import Protocol, runtime_checkable

from creek.classify.privacy_filter import PrivacyTierOverride
from creek.models import PrivacyTier


class LLMCallable(Protocol):
    """A prompt-completion callable returning the model's raw text."""

    def __call__(self, prompt: str) -> str:
        """Return the completion for *prompt*."""


@runtime_checkable
class FirstTokenReporting(Protocol):
    """A completion callable that can report its own time to first token.

    An optional sibling of :class:`LLMCallable`, probed structurally exactly
    as :class:`~creek_mcp.bench.usage.UsageReporting` is: a backend that does
    not stream is asked nothing, and its trials report ``ttft_s = None``.

    The figure is the *backend's own*, because only the backend sees the
    stream: :meth:`time_to_first_token` returns seconds from the start of the
    completion call to its first token. ADR-0016 R2 budgets time-to-first-token
    with "attestation round trip included", and for a pool client the dial,
    identity verification and attestation all happen inside that same call, so
    measuring from its start is what R2 asks for.

    Like ``token_usage``, it reads a figure already recorded, so it must not
    raise and must not perform I/O — the trial is decided before it is called.
    """

    def time_to_first_token(self) -> float | None:
        """Return the last completion's TTFT in seconds, or ``None``."""


class LLMFactory(Protocol):
    """Builds a tier-routed completion callable with a hard output ceiling."""

    def __call__(self, tier: PrivacyTier, *, max_tokens: int) -> LLMCallable:
        """Return a completion callable for *tier* bounded by *max_tokens*."""


class Retriever(Protocol):
    """The ``retrieve=`` seam of ``reflect_tool``: grounding lines for a query."""

    def __call__(
        self, query: str, vault: Path, override: PrivacyTierOverride, /
    ) -> list[str]:
        """Return grounding lines for *query* within *override*."""
