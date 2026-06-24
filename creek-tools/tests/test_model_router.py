"""Per-stage LLM routing schema + ModelRouter skeleton (#645).

The skeleton is purely additive: ``LLMRoutingConfig`` parses either the legacy
flat ``llm`` block (promoted to ``default``) or a per-stage map, and
``ModelRouter`` resolves ``(stage, tier) -> LLMConfig`` — every stage falling
back to ``default``. No call site is rewired here (that is #646); the tier arg
is accepted but not yet enforced (that is #647).
"""

from __future__ import annotations

from creek.classify.llm.router import ModelRouter
from creek.config import LLMConfig, LLMRoutingConfig
from creek.models import PrivacyTier

# --- LLMRoutingConfig schema + backward compatibility ----------------------


class TestLLMRoutingConfigBackCompat:
    """A legacy flat block is accepted and promoted to ``default``."""

    def test_flat_dict_promoted_to_default(self) -> None:
        """A legacy flat mapping becomes ``{default: <it>}``."""
        routing = LLMRoutingConfig.model_validate(
            {"provider": "anthropic", "model": "claude-sonnet-4-6"},
        )
        assert routing.default.provider == "anthropic"
        assert routing.default.model == "claude-sonnet-4-6"
        assert routing.classification is None

    def test_llmconfig_instance_promoted_to_default(self) -> None:
        """An ``LLMConfig`` instance (e.g. ``CreekConfig(llm=LLMConfig(...))``)."""
        routing = LLMRoutingConfig.model_validate(
            LLMConfig(provider="anthropic", model="x"),
        )
        assert routing.default.provider == "anthropic"

    def test_json_string_flat_round_trips(self) -> None:
        """The working env override shape ``CREEK_LLM='{"provider":…}'``."""
        routing = LLMRoutingConfig.model_validate_json('{"provider": "anthropic"}')
        assert routing.default.provider == "anthropic"

    def test_empty_defaults_to_local_ollama(self) -> None:
        """No config at all yields the local default."""
        routing = LLMRoutingConfig()
        assert routing.default.provider == "ollama"


class TestLLMRoutingConfigPerStage:
    """The per-stage map parses and ``for_stage`` falls back to ``default``."""

    def _routing(self) -> LLMRoutingConfig:
        return LLMRoutingConfig.model_validate(
            {
                "default": {"provider": "ollama", "model": "qwen3:8b"},
                "generation": {"provider": "anthropic", "model": "claude-sonnet-4-6"},
                "writing_desk": {"outliner": {"provider": "ollama", "model": "q"}},
            },
        )

    def test_per_stage_parses_independently(self) -> None:
        """Each configured stage keeps its own provider."""
        routing = self._routing()
        assert routing.for_stage("generation").provider == "anthropic"
        assert routing.for_stage("default").provider == "ollama"

    def test_unset_stage_falls_back_to_default(self) -> None:
        """A stage with no entry resolves to ``default``."""
        routing = self._routing()
        assert routing.for_stage("classification").provider == "ollama"
        assert routing.for_stage("frontend").provider == "ollama"

    def test_writing_desk_map_is_parsed(self) -> None:
        """The reserved ``writing_desk`` role map parses (wired in #648)."""
        routing = self._routing()
        assert routing.writing_desk["outliner"].provider == "ollama"

    def test_unknown_stage_resolves_to_default(self) -> None:
        """An unrecognised stage name is safe — resolves to ``default``."""
        routing = self._routing()
        assert routing.for_stage("nonsense").provider == "ollama"


# --- ModelRouter ------------------------------------------------------------


class TestModelRouter:
    """``ModelRouter`` resolves ``(stage, tier) -> LLMConfig``."""

    def test_from_flat_llmconfig_every_stage_is_default(self) -> None:
        """Constructed from a single LLMConfig, every stage resolves to it."""
        router = ModelRouter(LLMConfig(provider="ollama", model="qwen3:8b"))
        assert router.resolve("classification").provider == "ollama"
        assert router.resolve("generation").model == "qwen3:8b"

    def test_from_routing_config_resolves_per_stage(self) -> None:
        """Constructed from a routing config, stages resolve independently."""
        routing = LLMRoutingConfig.model_validate(
            {
                "default": {"provider": "ollama"},
                "generation": {"provider": "anthropic"},
            },
        )
        router = ModelRouter(routing)
        assert router.resolve("generation").provider == "anthropic"
        assert router.resolve("classification").provider == "ollama"

    def test_tier_arg_is_accepted_but_not_yet_enforced(self) -> None:
        """The tier arg is part of the signature now; enforcement is #647."""
        router = ModelRouter(LLMConfig(provider="anthropic"))
        # Skeleton: tier does not yet redirect (the Intimate chokepoint is #647).
        assert router.resolve("generation", PrivacyTier.INTIMATE).provider == (
            "anthropic"
        )

    def test_resolve_returns_an_llmconfig(self) -> None:
        """The resolved value is an ``LLMConfig`` ready for ``build_provider``."""
        router = ModelRouter(LLMConfig(provider="ollama"))
        assert isinstance(router.resolve("default"), LLMConfig)
