"""The model router: resolve ``(stage, tier) -> LLMConfig`` (#642).

``ModelRouter`` is the single chokepoint through which every LLM call site will
resolve which provider+model to use for a given pipeline stage. This skeleton
(#645) resolves each stage to its configured :class:`~creek.config.LLMConfig`,
falling back to the routing ``default``. The ``Intimate``-never-cloud
enforcement is wired into :meth:`resolve` in #647 (the ``tier`` argument is
accepted now so the signature is stable); the Writing Desk role map
(:meth:`resolve_role`) lands in #648.

Construction accepts either a :class:`~creek.config.LLMRoutingConfig` (the
per-stage shape) or a bare :class:`~creek.config.LLMConfig` (treated as the sole
``default``), so call sites can adopt the router before the config type changes.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from creek.config import LLMConfig, LLMRoutingConfig

if TYPE_CHECKING:
    from creek.models import PrivacyTier


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
        """Return the :class:`LLMConfig` for *stage*.

        Args:
            stage: The pipeline stage (``default`` / ``classification`` /
                ``generation`` / ``frontend``); unknown stages fall back to the
                routing ``default``.
            tier: The fragment's privacy tier. Accepted now for signature
                stability; the ``Intimate``-never-cloud redirect is enforced
                here in #647. Until then it has no effect.

        Returns:
            The resolved config, ready for
            :func:`~creek.classify.llm.providers.build_provider`.
        """
        # Issue #647: enforce Intimate-never-cloud here (redirect to local +
        # WARN; raise IntimateRoutingError if even ``default`` is cloud).
        _ = tier
        return self.routing.for_stage(stage)
