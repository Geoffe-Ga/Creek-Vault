"""Tests for the classification method-fidelity ladder (#736).

Covers the per-method ranking and ``best_available`` — including the
Intimate-never-cloud cap (Intimate is never proposed a cloud upgrade).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from creek.classify import fidelity as fid
from creek.config import CreekConfig, LLMConfig, LLMRoutingConfig
from creek.models import PrivacyTier

if TYPE_CHECKING:
    import pytest


class _StubProvider:
    """Minimal provider double with controllable availability + cloud flag."""

    def __init__(self, *, available: bool, is_cloud: bool) -> None:
        self.available = available
        self.is_cloud = is_cloud


def _config(*, classification: dict[str, str], default: dict[str, str]) -> CreekConfig:
    """A CreekConfig whose router resolves the given classification + default."""
    return CreekConfig(
        llm=LLMRoutingConfig(
            default=LLMConfig(**default),
            classification=LLMConfig(**classification),
        )
    )


def test_provider_is_cloud() -> None:
    """Only the known cloud providers are cloud; local/None are not."""
    assert fid.provider_is_cloud("anthropic")
    assert fid.provider_is_cloud("openai")
    assert fid.provider_is_cloud("gemini")
    assert not fid.provider_is_cloud("ollama")
    assert not fid.provider_is_cloud(None)


def test_method_rank_ladder() -> None:
    """The ladder orders unclassified < rules < local LLM < cloud LLM < manual."""
    assert fid.method_rank(None, None) == fid.RANK_UNCLASSIFIED
    assert fid.method_rank("rules", None) == fid.RANK_RULES
    assert fid.method_rank("llm", "ollama") == fid.RANK_LLM_LOCAL
    assert fid.method_rank("llm", None) == fid.RANK_LLM_LOCAL  # unknown → local rung
    assert fid.method_rank("llm", "anthropic") == fid.RANK_LLM_CLOUD
    assert fid.method_rank("manual", None) == fid.RANK_MANUAL
    # Strictly increasing.
    assert (
        fid.RANK_UNCLASSIFIED
        < fid.RANK_RULES
        < fid.RANK_LLM_LOCAL
        < fid.RANK_LLM_CLOUD
        < fid.RANK_MANUAL
    )


def test_best_available_caps_intimate_at_local(monkeypatch: pytest.MonkeyPatch) -> None:
    """A cloud classification stage gives cloud non-Intimate but local Intimate."""
    monkeypatch.setattr(
        fid,
        "build_provider",
        lambda cfg: _StubProvider(
            available=True, is_cloud=fid.provider_is_cloud(cfg.provider)
        ),
    )
    config = _config(
        classification={"provider": "anthropic", "model": "claude-haiku-4-5"},
        default={"provider": "ollama", "model": "qwen3:8b"},
    )
    best = fid.best_available(config)
    assert best.non_intimate == fid.RANK_LLM_CLOUD
    assert best.intimate == fid.RANK_LLM_LOCAL  # never cloud for Intimate
    assert best.any_llm_available()
    assert best.for_tier(PrivacyTier.INTIMATE) == fid.RANK_LLM_LOCAL
    assert best.for_tier(PrivacyTier.OPEN) == fid.RANK_LLM_CLOUD


def test_best_available_falls_back_to_rules_when_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unavailable cloud provider (no key/consent) credits only the rules rung."""
    monkeypatch.setattr(
        fid,
        "build_provider",
        lambda cfg: _StubProvider(available=False, is_cloud=True),
    )
    config = _config(
        classification={"provider": "anthropic", "model": "x"},
        default={"provider": "anthropic", "model": "x"},
    )
    best = fid.best_available(config)
    assert best.non_intimate == fid.RANK_RULES
    assert best.intimate == fid.RANK_RULES  # no local backend → Intimate is rules
    assert not best.any_llm_available()


def test_best_available_local_classification_is_local_both_tiers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A local classification stage gives the local-LLM rung for both tiers."""
    monkeypatch.setattr(
        fid,
        "build_provider",
        lambda cfg: _StubProvider(available=True, is_cloud=False),
    )
    config = _config(
        classification={"provider": "ollama", "model": "qwen3:8b"},
        default={"provider": "ollama", "model": "qwen3:8b"},
    )
    best = fid.best_available(config)
    assert best.non_intimate == fid.RANK_LLM_LOCAL
    assert best.intimate == fid.RANK_LLM_LOCAL
