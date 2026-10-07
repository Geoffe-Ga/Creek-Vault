"""Structural types for the model seam the harness drives.

These mirror the private ``_LLM`` / ``_LLMFactory`` protocols in
:mod:`creek_mcp.tools.reflect` structurally rather than importing them, so the
harness depends on the *shape* ``reflect_tool`` accepts and not on that
module's private names. Any object satisfying these satisfies reflect's.
Annotations are evaluated eagerly (no postponed annotations), so every name
they use is a real import.
"""

from pathlib import Path
from typing import Protocol

from creek.classify.privacy_filter import PrivacyTierOverride
from creek.models import PrivacyTier


class LLMCallable(Protocol):
    """A prompt-completion callable returning the model's raw text."""

    def __call__(self, prompt: str) -> str:
        """Return the completion for *prompt*."""


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
