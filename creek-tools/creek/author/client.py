"""Thin, mockable Anthropic client wrapper for the author desk (FEAT-041, #455).

The Writing Desk's only seam onto the network goes through this wrapper, so
unit tests inject a mock provider and never make a live call. The skeleton's
specialists/voice/reflection are pure stubs and do not yet call it; issue #460
wires it into the real Conductor.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from creek.classify.llm.providers import Completion, build_provider

if TYPE_CHECKING:
    from creek.classify.llm.base import LLMProvider
    from creek.classify.llm.router import ModelRouter
    from creek.config import AuthorConfig, LLMConfig
    from creek.models import PrivacyTier

_VOICE_ROLES = frozenset({"voice_drafter", "voice_line_editor"})
"""Writing Desk roles whose model the legacy ``AuthorConfig.voice_model``
field falls back to (#474) when the role has no ``writing_desk`` entry (#649)."""


def resolve_voice_model(author: AuthorConfig, llm: LLMConfig) -> str | None:
    """Resolve the model id for the voice call from config, never hard-coded.

    The desk's per-agent model tiers (#474) let an operator point the voice
    call at a different model than the rest of the pipeline. The voice tier
    falls back to the shared ``llm`` model when unset, so no model id is
    literal in :mod:`creek.author`.

    Args:
        author: The author-subsystem config carrying the per-agent overrides.
        llm: The shared LLM config supplying the fallback model id.

    Returns:
        :attr:`AuthorConfig.voice_model` when set, otherwise ``llm.model`` —
        which may itself be ``None``, meaning "use the provider's default";
        :meth:`AuthorLLMClient.from_config` passes ``None`` through untouched.
    """
    return author.voice_model or llm.model


class AuthorLLMClient:
    """A thin wrapper over an :class:`LLMProvider` for author-desk calls.

    Attributes:
        _provider: The underlying provider performing the completion call.
    """

    def __init__(self, provider: LLMProvider) -> None:
        """Store the provider this client delegates to.

        Args:
            provider: The provider performing the actual completion call.
        """
        self._provider = provider

    @classmethod
    def from_config(
        cls, config: LLMConfig, *, model: str | None = None
    ) -> AuthorLLMClient:
        """Build a client from *config*, sourcing the model id from config.

        The backend is selected by :func:`build_provider` so the desk honors
        the operator's ``provider`` choice instead of being hard-wired to
        Anthropic (#605).

        Args:
            config: The LLM configuration (provider + model id come from here,
                never hard-coded).
            model: Optional per-agent model override (e.g. the resolved voice
                tier). When supplied it replaces ``config.model`` so the desk
                can run a different model per agent without any literal id in
                :mod:`creek.author`. ``None`` keeps ``config.model`` unchanged.

        Returns:
            An :class:`AuthorLLMClient` wrapping a configured provider.
        """
        effective = (
            config if model is None else config.model_copy(update={"model": model})
        )
        return cls(build_provider(effective))

    @classmethod
    def for_role(
        cls,
        router: ModelRouter,
        role: str,
        *,
        author: AuthorConfig | None = None,
        tier: PrivacyTier | None = None,
    ) -> AuthorLLMClient:
        """Build a client for a Writing Desk *role* via the router (#649).

        Resolves the role's provider+model through
        :meth:`~creek.classify.llm.router.ModelRouter.resolve_role` — so the
        role inherits the per-stage routing **and** the ``Intimate``-never-cloud
        chokepoint. Precedence for the voice roles (decision #2): an explicit
        ``writing_desk`` entry wins; otherwise the legacy
        :attr:`AuthorConfig.voice_model` fills the model id; otherwise the
        generation/default model stands. Non-voice roles have no legacy field.

        Args:
            router: The run's :class:`ModelRouter`.
            role: The subagent role (e.g. ``outliner`` / ``voice_drafter``).
            author: Author-subsystem config carrying the legacy per-agent model
                overrides; ``None`` skips the legacy fallback.
            tier: The fragment's privacy tier (gated by the chokepoint).

        Returns:
            An :class:`AuthorLLMClient` wrapping the role's resolved provider.

        Raises:
            IntimateRoutingError: Propagated from ``resolve_role`` when an
                ``Intimate`` role is cloud with no local fallback.
        """
        cfg = router.resolve_role(role, tier)
        if (
            author is not None
            and role in _VOICE_ROLES
            and role not in router.routing.writing_desk
            and author.voice_model is not None
        ):
            cfg = cfg.model_copy(update={"model": author.voice_model})
        return cls(build_provider(cfg))

    def complete(self, prompt: str, *, max_tokens: int | None = None) -> str:
        """Return the completion text for *prompt*.

        Args:
            prompt: The prompt to send.
            max_tokens: Optional output-token ceiling.

        Returns:
            The provider's completion text.
        """
        return self._provider.complete(prompt, max_tokens=max_tokens).text

    def complete_with_usage(
        self,
        prompt: str,
        *,
        system: str | None = None,
        max_tokens: int | None = None,
    ) -> Completion:
        """Return the full completion (text + token usage) for *prompt*.

        Unlike :meth:`complete` (which yields only the text and is kept stable
        for existing callers), this exposes the SDK's token usage so the desk
        can surface a run's cost and cache-hit rate. A static *system* prefix is
        sent as an ephemeral cached block by a cloud provider that supports it
        (#474); local providers ignore it.

        Args:
            prompt: The dynamic user prompt.
            system: Optional static system prefix to cache.
            max_tokens: Optional output-token ceiling.

        Returns:
            The provider's :class:`Completion`, carrying text and
            :attr:`~Completion.usage`.
        """
        return self._provider.complete(
            prompt,
            system=system,
            max_tokens=max_tokens,
        )
