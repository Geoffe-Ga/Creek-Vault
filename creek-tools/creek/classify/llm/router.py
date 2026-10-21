"""The model router: resolve ``(stage, tier) -> LLMConfig`` (#642).

``ModelRouter`` is the single chokepoint through which every LLM call site
resolves which provider+model to use for a given pipeline stage, and the single
place the ``Intimate``-never-off-device privacy invariant is enforced (#647):
an ``Intimate``-tier fragment is redirected off any provider that is not
intimate-eligible to the local ``default`` (with a WARNING), or — when even
``default`` is ineligible — refused loudly via :class:`IntimateRoutingError`.
Call sites must never re-check the tier themselves; they pass it to
:meth:`resolve` and trust the result.

The gate reads ``intimate_eligible``, not ``is_cloud`` (widened by #1894,
ADR-0016 Decision 6). The shared confidential inference pool is not cloud —
no operator key, no consent variable — and still never sees ``Intimate``
content, because by the time the pool could refuse a tier, the plaintext
would already have left the vault. Enforcing it here is what makes the pool's
own refusal defence in depth rather than the first line.

Construction accepts either a :class:`~creek.config.LLMRoutingConfig` (the
per-stage shape) or a bare :class:`~creek.config.LLMConfig` (treated as the sole
``default``), so call sites can adopt the router before the config type changes.
The Writing Desk role map (:meth:`resolve_role`) lands in #648.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from creek.classify.llm.providers import (
    build_provider,
    provider_intimate_eligible,
    provider_is_cloud,
)
from creek.config import LLMConfig, LLMRoutingConfig
from creek.models import PrivacyTier

if TYPE_CHECKING:
    from creek.classify.llm.base import LLMProvider

logger = logging.getLogger(__name__)


def _ineligible_kind(provider: str) -> str:
    """Name why *provider* may not be shown ``Intimate`` content.

    Args:
        provider: The ``LLMConfig.provider`` string.

    Returns:
        ``"cloud"`` for a readable cloud vendor, else ``"off-device"`` — the
        shared inference pool is not cloud (no operator key, no consent
        variable) yet still never sees ``Intimate`` content.
    """
    return "cloud" if provider_is_cloud(provider) else "off-device"


class IntimateRoutingError(RuntimeError):
    """Raised when an ``Intimate`` fragment cannot be routed to any local model.

    The router redirects ``Intimate``-tier work off every provider that is not
    intimate-eligible — cloud vendors and the shared inference pool alike — to
    the local ``default``; this is raised only when ``default`` is *also*
    ineligible, so there is no safe local backend, and failing loudly is
    preferable to silently egressing intimate content.

    Attributes:
        stage_provider: The ineligible provider the stage was configured with.
    """

    def __init__(self, stage_provider: str) -> None:
        """Build the error naming the offending provider and the remedy.

        Args:
            stage_provider: The ineligible provider configured for the stage.
        """
        self.stage_provider = stage_provider
        super().__init__(
            f"Intimate-tier fragment cannot route to "
            f"{_ineligible_kind(stage_provider)} provider {stage_provider!r}, "
            "and llm.default is not intimate-eligible either, so there is no "
            "local backend to fall back to. Set llm.default to a local provider "
            "(e.g. ollama) to process Intimate-tier content.",
        )


class ModelRouter:
    """Resolve a pipeline stage (and fragment tier) to an :class:`LLMConfig`.

    Attributes:
        routing: The normalised per-stage routing config.
    """

    def __init__(self, routing: LLMRoutingConfig | LLMConfig) -> None:
        """Build a router from a routing config or a single flat config.

        Args:
            routing: A :class:`LLMRoutingConfig`, or a bare :class:`LLMConfig`
                which is wrapped as the routing ``default`` (every stage then
                resolves to it).
        """
        self.routing: LLMRoutingConfig = (
            routing
            if isinstance(routing, LLMRoutingConfig)
            else LLMRoutingConfig(default=routing)
        )

    def resolve(self, stage: str, tier: PrivacyTier | None = None) -> LLMConfig:
        """Return the :class:`LLMConfig` for *stage*, honoring the tier gate.

        Args:
            stage: The pipeline stage (``default`` / ``classification`` /
                ``generation`` / ``frontend``); unknown stages fall back to the
                routing ``default``.
            tier: The fragment's privacy tier. When ``Intimate`` and the stage
                resolves to a cloud provider, the result is redirected to the
                local ``default`` (see :meth:`_enforce_local_for_intimate`).

        Returns:
            The resolved config, ready for
            :func:`~creek.classify.llm.providers.build_provider`.

        Raises:
            IntimateRoutingError: When *tier* is ``Intimate``, the stage is
                cloud, and no local ``default`` exists to redirect to.
        """
        return self._enforce_local_for_intimate(self.routing.for_stage(stage), tier)

    def resolve_role(self, role: str, tier: PrivacyTier | None = None) -> LLMConfig:
        """Return the :class:`LLMConfig` for a Writing Desk *role* (#648).

        Resolution falls back: the ``writing_desk`` role entry → the
        ``generation`` stage → the routing ``default``. The same
        ``Intimate``-never-cloud gate as :meth:`resolve` is applied, so every
        subagent role inherits the privacy guarantee with no extra tier check.

        Args:
            role: The subagent role (e.g. ``outliner`` / ``voice_drafter``).
            tier: The fragment's privacy tier (see :meth:`resolve`).

        Returns:
            The resolved config for *role*.

        Raises:
            IntimateRoutingError: When *tier* is ``Intimate``, the role is
                cloud, and no local ``default`` exists to redirect to.
        """
        cfg = self.routing.writing_desk.get(role) or self.routing.for_stage(
            "generation",
        )
        return self._enforce_local_for_intimate(cfg, tier)

    def provider_for(
        self,
        stage: str,
        tier: PrivacyTier | None = None,
    ) -> LLMProvider:
        """Build the provider for *stage* (and *tier*) via the factory.

        Args:
            stage: The pipeline stage.
            tier: The fragment's privacy tier (see :meth:`resolve`).

        Returns:
            A constructed :class:`~creek.classify.llm.base.LLMProvider` — a local
            one whenever the tier gate redirected an ``Intimate`` fragment.
        """
        return build_provider(self.resolve(stage, tier))

    def _enforce_local_for_intimate(
        self,
        cfg: LLMConfig,
        tier: PrivacyTier | None,
    ) -> LLMConfig:
        """Apply the ``Intimate``-never-off-device rule — the ONLY tier gate.

        Non-``Intimate`` tiers (and ``None``) pass through unchanged. An
        ``Intimate`` fragment bound for a provider that is not
        intimate-eligible is redirected to the local ``default`` with a single
        WARNING; if ``default`` is itself ineligible,
        :class:`IntimateRoutingError` is raised rather than emitting the call.

        The test is
        :func:`~creek.classify.llm.providers.provider_intimate_eligible`, not
        ``is_cloud`` (#647, widened by #1894): the shared inference pool is not
        cloud — no operator key, no consent variable — and still never sees
        ``Intimate`` content (ADR-0016 Decision 5). The same test governs the
        ``default`` rescue target, so a ``default`` stage routed to the pool is
        never redirected *to*.

        Args:
            cfg: The stage's resolved config.
            tier: The fragment's privacy tier.

        Returns:
            *cfg* unchanged, or the local ``default`` when redirecting.

        Raises:
            IntimateRoutingError: When no local backend exists to redirect to.
        """
        if tier != PrivacyTier.INTIMATE or provider_intimate_eligible(cfg.provider):
            return cfg
        local = self.routing.for_stage("default")
        if not provider_intimate_eligible(local.provider):
            raise IntimateRoutingError(stage_provider=cfg.provider)
        logger.warning(
            "Intimate-tier fragment: redirecting %s provider %r to local %r "
            "(off-device egress refused at the routing chokepoint).",
            _ineligible_kind(cfg.provider),
            cfg.provider,
            local.provider,
        )
        return local
