"""Classification method-fidelity ladder (#736).

Single source of truth for ranking *how* a fragment was classified, and for
computing the best method available to a vault right now. A quality-aware re-run
(``creek fill --upgrade``) uses this to decide when re-classifying would actually
improve an artifact rather than churn it.

The ladder, lowest to highest fidelity::

    unclassified < rules < local LLM < cloud LLM < manual

``manual`` sits at the top so a human classification is never auto-overwritten.
The Intimate tier's ceiling is the best **local** method — Intimate content is
never ranked or routed to a cloud provider (the ModelRouter chokepoint enforces
the routing; this module never proposes a cloud upgrade for Intimate).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from creek.classify.constants import LLM_METHOD, MANUAL_METHOD, RULES_METHOD
from creek.classify.llm.providers import build_provider
from creek.classify.llm.router import IntimateRoutingError
from creek.models import PrivacyTier

if TYPE_CHECKING:
    from creek.config import CreekConfig, LLMConfig

CLOUD_PROVIDERS: frozenset[str] = frozenset({"anthropic", "openai", "gemini"})
"""Provider ids treated as cloud (the rung above a local LLM)."""

RANK_UNCLASSIFIED: int = 0
RANK_RULES: int = 1
RANK_LLM_LOCAL: int = 2
RANK_LLM_CLOUD: int = 3
RANK_MANUAL: int = 4

_RANK_LABELS: dict[int, str] = {
    RANK_UNCLASSIFIED: "unclassified",
    RANK_RULES: "rules",
    RANK_LLM_LOCAL: "local LLM",
    RANK_LLM_CLOUD: "cloud LLM",
    RANK_MANUAL: "manual",
}


def provider_is_cloud(provider: str | None) -> bool:
    """Return whether *provider* names a cloud backend."""
    return bool(provider) and provider in CLOUD_PROVIDERS


def rank_label(rank: int) -> str:
    """Return a human label for a fidelity *rank*."""
    return _RANK_LABELS.get(rank, "unknown")


def method_rank(method: str | None, provider: str | None) -> int:
    """Return the fidelity rank of a classification.

    Args:
        method: The ``classification_method`` value (``rules`` / ``llm`` /
            ``manual``), or ``None`` for an unclassified fragment.
        provider: The ``classification_provider`` value (only meaningful for
            ``llm``); decides the local vs cloud rung.

    Returns:
        A rank from :data:`RANK_UNCLASSIFIED` to :data:`RANK_MANUAL`.
    """
    if method == MANUAL_METHOD:
        return RANK_MANUAL
    if method == LLM_METHOD:
        return RANK_LLM_CLOUD if provider_is_cloud(provider) else RANK_LLM_LOCAL
    if method == RULES_METHOD:
        return RANK_RULES
    return RANK_UNCLASSIFIED


def _available_rank(config: LLMConfig) -> int:
    """Return the rank an LLM stage *config* can deliver right now.

    Falls back to :data:`RANK_RULES` when the provider is unavailable (no key /
    consent for a cloud provider, or an unreachable local server) — i.e. the LLM
    rung is only credited when a real classification could actually run.
    """
    is_cloud = provider_is_cloud(config.provider)
    try:
        available = build_provider(config).available
    except RuntimeError:
        # Cloud providers validate key/consent in their constructor.
        available = False
    if not available:
        return RANK_RULES
    return RANK_LLM_CLOUD if is_cloud else RANK_LLM_LOCAL


@dataclass(frozen=True)
class BestAvailable:
    """The best classification rank available per tier, given config + env."""

    non_intimate: int
    intimate: int

    def for_tier(self, tier: PrivacyTier) -> int:
        """Return the best available rank for a fragment's *tier*."""
        return self.intimate if tier == PrivacyTier.INTIMATE else self.non_intimate

    def any_llm_available(self) -> bool:
        """Whether either tier can reach an LLM rung (above plain rules)."""
        return max(self.non_intimate, self.intimate) > RANK_RULES


def best_available(config: CreekConfig) -> BestAvailable:
    """Compute the best classification rank available to *config* per tier.

    Resolves the ``classification`` stage through the ModelRouter exactly as
    :func:`creek.classify.classify_engine.build_tier_classifiers` does — the
    non-Intimate route uses the configured provider; the Intimate route is
    redirected to a local provider, and its ceiling is capped at
    :data:`RANK_LLM_LOCAL` so an upgrade is never proposed to cloud for Intimate.

    Args:
        config: The loaded Creek configuration (carries the model router).

    Returns:
        The :class:`BestAvailable` ranks for non-Intimate and Intimate tiers.
    """
    router = config.model_router
    non_intimate = _available_rank(router.resolve("classification"))
    try:
        intimate_cfg = router.resolve("classification", PrivacyTier.INTIMATE)
    except IntimateRoutingError:
        # No local backend for Intimate → it can only be rules-classified.
        intimate = RANK_RULES
    else:
        intimate = min(_available_rank(intimate_cfg), RANK_LLM_LOCAL)
    return BestAvailable(non_intimate=non_intimate, intimate=intimate)
