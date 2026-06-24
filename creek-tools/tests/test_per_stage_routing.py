"""Per-stage routing wired into CreekConfig + call sites (#646).

#645 added ``LLMRoutingConfig`` + ``ModelRouter`` as standalone machinery.
This issue makes ``CreekConfig.llm`` a ``LLMRoutingConfig`` and routes every LLM
call site through ``CreekConfig.model_router`` — so classification and generation
can use different providers in one run, while a legacy flat ``llm`` block keeps
working unchanged.
"""

from __future__ import annotations

import pytest
import typer

from creek.classify.llm.providers import OllamaProvider, build_provider
from creek.classify.llm.router import ModelRouter
from creek.compile.engine import default_llm
from creek.config import CreekConfig, LLMConfig


class TestCreekConfigRouting:
    """``CreekConfig.llm`` is per-stage; ``model_router`` resolves it."""

    def test_model_router_is_a_router(self) -> None:
        """The property hands back a ModelRouter over the config's routing."""
        config = CreekConfig()
        assert isinstance(config.model_router, ModelRouter)

    def test_per_stage_config_resolves_distinct_providers(self) -> None:
        """classification -> ollama and generation -> anthropic in one config."""
        config = CreekConfig.model_validate(
            {
                "llm": {
                    "default": {"provider": "ollama", "model": "qwen3:8b"},
                    "classification": {"provider": "ollama", "model": "qwen3:8b"},
                    "generation": {
                        "provider": "anthropic",
                        "model": "claude-sonnet-4-6",
                    },
                },
            },
        )
        router = config.model_router
        assert router.resolve("classification").provider == "ollama"
        assert router.resolve("generation").provider == "anthropic"

    def test_legacy_flat_block_feeds_every_stage(self) -> None:
        """A pre-#642 flat ``llm`` block resolves identically for all stages."""
        config = CreekConfig.model_validate(
            {"llm": {"provider": "anthropic", "model": "claude-sonnet-4-6"}},
        )
        router = config.model_router
        assert router.resolve("classification").provider == "anthropic"
        assert router.resolve("generation").provider == "anthropic"
        assert config.llm.default.model == "claude-sonnet-4-6"

    def test_llmconfig_instance_still_accepted(self) -> None:
        """``CreekConfig(llm=LLMConfig(...))`` keeps working (promoted to default)."""
        config = CreekConfig(llm=LLMConfig(provider="anthropic"))
        assert config.model_router.resolve("generation").provider == "anthropic"


class TestPreflightEnumeratesStages:
    """The consent preflight must see a cloud provider in ANY stage."""

    def test_all_configs_includes_every_stage(self) -> None:
        """``all_configs`` surfaces default + scalar stages + writing_desk."""
        config = CreekConfig.model_validate(
            {
                "llm": {
                    "default": {"provider": "ollama"},
                    "generation": {"provider": "anthropic"},
                    "writing_desk": {"voice_drafter": {"provider": "openai"}},
                },
            },
        )
        providers = {cfg.provider for cfg in config.llm.all_configs()}
        assert providers == {"ollama", "anthropic", "openai"}

    def test_preflight_exits_on_cloud_in_nondefault_stage(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A cloud provider on a NON-default stage still triggers the gate."""
        from creek import cli as cli_module

        monkeypatch.delenv("CREEK_CLOUD_CONSENT", raising=False)
        monkeypatch.delenv("CREEK_ANTHROPIC_CONSENT", raising=False)
        config = CreekConfig.model_validate(
            {
                "llm": {
                    "default": {"provider": "ollama"},
                    "classification": {"provider": "anthropic"},
                },
            },
        )
        with pytest.raises(typer.Exit) as exc:
            cli_module._preflight_cloud_consent(config)
        assert exc.value.exit_code == 1

    def test_preflight_noops_when_every_stage_is_local(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """All-local stages never gate, even without consent set."""
        from creek import cli as cli_module

        monkeypatch.delenv("CREEK_CLOUD_CONSENT", raising=False)
        monkeypatch.delenv("CREEK_ANTHROPIC_CONSENT", raising=False)
        config = CreekConfig.model_validate(
            {"llm": {"default": {"provider": "ollama"}}},
        )
        # Must not raise.
        cli_module._preflight_cloud_consent(config)


class TestGenerationFollowsConfig:
    """``default_llm`` routes via the factory, not hard-wired Anthropic (#646)."""

    def test_factory_routes_generation_provider(self) -> None:
        """The factory the generation path uses honors the configured provider."""
        # build_provider(ollama) needs no API key/consent — proving generation
        # is no longer hard-wired to Anthropic.
        provider = build_provider(LLMConfig(provider="ollama", model="qwen3:8b"))
        assert isinstance(provider, OllamaProvider)

    def test_default_llm_completes_via_provider_neutral_protocol(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``default_llm`` drives the provider's ``complete`` (not ``.call``)."""

        class _FakeProvider:
            def complete(self, prompt: str, **_kw: object) -> object:
                return type("C", (), {"text": f"echo:{prompt}"})()

        # Patch the factory at its source so the lazy import inside default_llm
        # resolves to the fake (no network, no Anthropic hard-wire).
        monkeypatch.setattr(
            "creek.classify.llm.build_provider",
            lambda _config: _FakeProvider(),
        )
        call = default_llm(LLMConfig(provider="ollama"))
        assert call("hi") == "echo:hi"
