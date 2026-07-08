"""Writing Desk role->model routing through the chokepoint (#648).

``ModelRouter.resolve_role`` resolves a subagent role (outliner, voice_drafter,
…) to an ``LLMConfig`` via the fallback chain role -> ``generation`` stage ->
``default``, reusing the #647 Intimate-never-cloud gate so every role inherits
the privacy guarantee for free.
"""

from __future__ import annotations

import pytest

from creek.classify.llm.router import IntimateRoutingError, ModelRouter
from creek.config import LLMRoutingConfig
from creek.models import PrivacyTier


def _router(**llm: object) -> ModelRouter:
    return ModelRouter(LLMRoutingConfig.model_validate(llm))


class TestResolveRoleFallback:
    """role -> generation -> default."""

    def test_configured_role_wins(self) -> None:
        """A role with its own entry resolves to that provider."""
        router = _router(
            default={"provider": "ollama"},
            generation={"provider": "anthropic"},
            writing_desk={"voice_drafter": {"provider": "openai", "model": "gpt-5.4"}},
        )
        assert router.resolve_role("voice_drafter").provider == "openai"

    def test_unset_role_falls_back_to_generation(self) -> None:
        """A role with no entry uses the generation stage."""
        router = _router(
            default={"provider": "ollama"},
            generation={"provider": "anthropic"},
            writing_desk={"voice_drafter": {"provider": "openai"}},
        )
        assert router.resolve_role("outliner").provider == "anthropic"

    def test_unset_role_no_generation_falls_back_to_default(self) -> None:
        """With neither a role entry nor a generation stage, default applies."""
        router = _router(default={"provider": "ollama"})
        assert router.resolve_role("researcher").provider == "ollama"


class TestRoleInheritsIntimateGate:
    """Roles route through the same #647 chokepoint."""

    def test_intimate_role_redirects_to_local(self) -> None:
        """An Intimate fragment on a cloud role redirects to the local default."""
        router = _router(
            default={"provider": "ollama"},
            writing_desk={"voice_drafter": {"provider": "anthropic"}},
        )
        assert router.resolve_role("voice_drafter").provider == "anthropic"
        assert (
            router.resolve_role(
                "voice_drafter",
                PrivacyTier.INTIMATE,
            ).provider
            == "ollama"
        )

    def test_intimate_role_raises_when_default_is_cloud(self) -> None:
        """No local fallback for a role either -> fail loudly."""
        router = _router(
            default={"provider": "anthropic"},
            writing_desk={"voice_drafter": {"provider": "openai"}},
        )
        with pytest.raises(IntimateRoutingError):
            router.resolve_role("voice_drafter", PrivacyTier.INTIMATE)
