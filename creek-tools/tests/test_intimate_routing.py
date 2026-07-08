"""Intimate-never-cloud enforcement at the routing chokepoint (#647).

The non-negotiable privacy invariant: an ``Intimate``-tier fragment must never
route to a cloud provider. Enforced in exactly one place —
``ModelRouter._enforce_local_for_intimate`` — which redirects to the local
``default`` (with a WARNING) or, if even ``default`` is cloud, raises loudly.
These tests assert on *provider construction*, not just a returned string, so a
regression that re-enables cloud egress fails the guard.
"""

from __future__ import annotations

import logging

import pytest

from creek.classify.llm.providers import AnthropicProvider, OllamaProvider
from creek.classify.llm.router import IntimateRoutingError, ModelRouter
from creek.config import LLMRoutingConfig
from creek.models import PrivacyTier


def _router(**stages: dict[str, str]) -> ModelRouter:
    """Build a router from a per-stage mapping of ``{stage: {provider: ...}}``."""
    return ModelRouter(LLMRoutingConfig.model_validate(stages))


class TestIntimateNeverRoutesToCloud:
    """The named guard + its enforcement semantics."""

    def test_intimate_tier_never_routes_to_cloud(self) -> None:
        """Intimate + a cloud-configured stage resolves to the local default."""
        router = _router(
            default={"provider": "ollama", "model": "qwen3:8b"},
            generation={"provider": "anthropic", "model": "claude-sonnet-4-6"},
        )
        resolved = router.resolve("generation", PrivacyTier.INTIMATE)
        # Redirected to the local default — NOT the configured anthropic.
        assert resolved.provider == "ollama"
        # And constructing the provider yields a LOCAL one — no cloud egress.
        provider = router.provider_for("generation", PrivacyTier.INTIMATE)
        assert isinstance(provider, OllamaProvider)
        assert not isinstance(provider, AnthropicProvider)

    def test_warns_on_redirect(self, caplog: pytest.LogCaptureFixture) -> None:
        """The redirect emits one WARNING for the audit trail."""
        router = _router(
            default={"provider": "ollama"},
            generation={"provider": "anthropic"},
        )
        with caplog.at_level(logging.WARNING):
            router.resolve("generation", PrivacyTier.INTIMATE)
        assert any(
            record.levelno == logging.WARNING and "intimate" in record.message.lower()
            for record in caplog.records
        )

    def test_raises_when_even_default_is_cloud(self) -> None:
        """No safe local exists → fail loudly rather than silently egress."""
        router = _router(
            default={"provider": "anthropic"},
            generation={"provider": "anthropic"},
        )
        with pytest.raises(IntimateRoutingError):
            router.resolve("generation", PrivacyTier.INTIMATE)

    def test_non_intimate_honors_configured_cloud(self) -> None:
        """OPEN/PERSONAL/None still route to the configured cloud provider."""
        router = _router(
            default={"provider": "ollama"},
            generation={"provider": "anthropic"},
        )
        assert router.resolve("generation", PrivacyTier.OPEN).provider == "anthropic"
        assert router.resolve("generation", PrivacyTier.PERSONAL).provider == (
            "anthropic"
        )
        # The contrapositive of the guard: WITHOUT the Intimate tier the cloud
        # provider IS returned — proving the redirect is tier-gated, not blanket.
        assert router.resolve("generation").provider == "anthropic"

    def test_intimate_on_local_stage_is_unchanged(self) -> None:
        """Intimate + an already-local stage needs no redirect."""
        router = _router(default={"provider": "ollama"})
        assert router.resolve("classification", PrivacyTier.INTIMATE).provider == (
            "ollama"
        )
