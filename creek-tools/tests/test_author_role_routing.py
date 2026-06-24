"""Author Writing Desk roles resolve through the router (#649).

``AuthorLLMClient.for_role`` resolves a subagent role through the
``ModelRouter`` — inheriting per-stage routing and the #647 Intimate gate — with
the legacy ``AuthorConfig.voice_model`` as the fallback for the voice roles
(decision #2: an explicit ``writing_desk`` entry wins).

Note: the desk's conductor/specialists are still stubs (the LLM path is an
injected seam; #460 wires the real consumer), so these tests pin the resolution
layer #460 will consume, capturing the ``LLMConfig`` handed to the factory.
"""

from __future__ import annotations

import pytest

from creek.author.client import AuthorLLMClient, resolve_voice_model
from creek.classify.llm.router import IntimateRoutingError, ModelRouter
from creek.config import AuthorConfig, LLMConfig, LLMRoutingConfig
from creek.models import PrivacyTier


@pytest.fixture
def captured_config(monkeypatch: pytest.MonkeyPatch) -> dict[str, LLMConfig]:
    """Capture the ``LLMConfig`` ``for_role`` hands to ``build_provider``."""
    captured: dict[str, LLMConfig] = {}

    def _fake_build(cfg: LLMConfig) -> object:
        captured["cfg"] = cfg
        return object()

    monkeypatch.setattr("creek.author.client.build_provider", _fake_build)
    return captured


def _router() -> ModelRouter:
    return ModelRouter(
        LLMRoutingConfig.model_validate(
            {
                "default": {"provider": "ollama", "model": "qwen3:8b"},
                "generation": {"provider": "anthropic", "model": "claude-sonnet-4-6"},
                "writing_desk": {
                    "outliner": {"provider": "ollama", "model": "qwen3:8b"},
                    "voice_drafter": {"provider": "openai", "model": "gpt-5.4"},
                },
            },
        ),
    )


class TestForRoleResolution:
    """``for_role`` honors the role map, fallbacks, and voice_model precedence."""

    def test_writing_desk_entry_wins(
        self,
        captured_config: dict[str, LLMConfig],
    ) -> None:
        """A configured role uses its own provider+model."""
        AuthorLLMClient.for_role(_router(), "voice_drafter")
        assert captured_config["cfg"].provider == "openai"
        assert captured_config["cfg"].model == "gpt-5.4"

    def test_unset_role_falls_back_to_generation(
        self,
        captured_config: dict[str, LLMConfig],
    ) -> None:
        """A role with no entry uses the generation stage."""
        AuthorLLMClient.for_role(_router(), "fact_checker")
        assert captured_config["cfg"].provider == "anthropic"

    def test_voice_model_fills_unset_voice_role(
        self,
        captured_config: dict[str, LLMConfig],
    ) -> None:
        """Legacy voice_model fills the model for an unset voice role."""
        router = ModelRouter(
            LLMRoutingConfig.model_validate(
                {"generation": {"provider": "anthropic", "model": "claude-sonnet-4-6"}},
            ),
        )
        author = AuthorConfig(voice_model="claude-opus-4-8")
        AuthorLLMClient.for_role(router, "voice_line_editor", author=author)
        assert captured_config["cfg"].provider == "anthropic"
        assert captured_config["cfg"].model == "claude-opus-4-8"

    def test_explicit_role_beats_voice_model(
        self,
        captured_config: dict[str, LLMConfig],
    ) -> None:
        """An explicit writing_desk.voice_drafter wins over voice_model."""
        author = AuthorConfig(voice_model="claude-opus-4-8")
        AuthorLLMClient.for_role(_router(), "voice_drafter", author=author)
        assert captured_config["cfg"].model == "gpt-5.4"  # role map, not voice_model

    def test_voice_model_not_applied_to_non_voice_role(
        self,
        captured_config: dict[str, LLMConfig],
    ) -> None:
        """voice_model is a voice-role fallback only — not for e.g. researcher."""
        router = ModelRouter(
            LLMRoutingConfig.model_validate(
                {"generation": {"provider": "anthropic", "model": "claude-sonnet-4-6"}},
            ),
        )
        author = AuthorConfig(voice_model="claude-opus-4-8")
        AuthorLLMClient.for_role(router, "researcher", author=author)
        assert captured_config["cfg"].model == "claude-sonnet-4-6"  # not voice_model


class TestForRoleInheritsIntimateGate:
    """Roles route through the #647 chokepoint."""

    def test_intimate_role_redirects_to_local(
        self,
        captured_config: dict[str, LLMConfig],
    ) -> None:
        """An Intimate fragment on a cloud role builds the local provider."""
        AuthorLLMClient.for_role(_router(), "voice_drafter", tier=PrivacyTier.INTIMATE)
        assert captured_config["cfg"].provider == "ollama"  # redirected from openai

    def test_intimate_role_raises_without_local_default(self) -> None:
        """No local fallback → fail loudly (no client built)."""
        router = ModelRouter(
            LLMRoutingConfig.model_validate(
                {
                    "default": {"provider": "anthropic"},
                    "writing_desk": {"voice_drafter": {"provider": "openai"}},
                },
            ),
        )
        with pytest.raises(IntimateRoutingError):
            AuthorLLMClient.for_role(
                router,
                "voice_drafter",
                tier=PrivacyTier.INTIMATE,
            )


class TestLegacyResolveVoiceModel:
    """The legacy resolver is unchanged (back-compat)."""

    def test_voice_model_wins_then_llm_model(self) -> None:
        """resolve_voice_model still returns voice_model or the llm fallback."""
        llm = LLMConfig(provider="anthropic", model="claude-sonnet-4-6")
        assert resolve_voice_model(AuthorConfig(voice_model="x"), llm) == "x"
        assert resolve_voice_model(AuthorConfig(), llm) == "claude-sonnet-4-6"
