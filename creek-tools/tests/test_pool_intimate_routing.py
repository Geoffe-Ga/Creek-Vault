"""INTIMATE never reaches the shared inference pool (#1894, ADR-0016 T6).

ADR-0016 Decision 5 is a hard invariant under either implementation: INTIMATE
content never leaves the vault process, attested pool or not. Decision 6 names
where that is enforced — the one
:class:`~creek.classify.llm.router.ModelRouter` chokepoint, on the
``intimate_eligible`` flag, exactly as it was already enforced on ``is_cloud``.

The pool's *own* tier refusal would be too late to be the first line: by the
time the pool could refuse, the plaintext would already have left. So every
test here asserts on a socket spy as well as on the routing decision — the
claim is "zero dials", not "the pool said no".
"""

from __future__ import annotations

import logging

import pytest

from creek.classify.llm.pool_boundary import POOL_URL_ENV
from creek.classify.llm.providers import OllamaProvider, PoolProvider
from creek.classify.llm.router import IntimateRoutingError, ModelRouter
from creek.config import LLMConfig, LLMRoutingConfig
from creek.models import PrivacyTier
from tests.pool_support import SocketSpy

_LOOPBACK = "http://127.0.0.1:11434"


def _pool() -> LLMConfig:
    """Return a stage routed to the shared inference pool."""
    return LLMConfig(provider="pool", model="creek-test-model:q4")


def _local() -> LLMConfig:
    """Return a stage routed to the loopback Ollama runtime."""
    return LLMConfig(provider="ollama", ollama_url=_LOOPBACK)


@pytest.fixture(autouse=True)
def _pinned_pool(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin a destination so a leak would have somewhere to go."""
    monkeypatch.setenv(POOL_URL_ENV, "https://pool.vendor.test")


def test_an_intimate_fragment_on_a_pool_stage_is_redirected_to_local(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The chokepoint swaps the pool for the local default, before any dial."""
    spy = SocketSpy(monkeypatch)
    router = ModelRouter(
        LLMRoutingConfig(default=_local(), generation=_pool()),
    )

    with caplog.at_level(logging.WARNING):
        resolved = router.resolve("generation", PrivacyTier.INTIMATE)
        provider = router.provider_for("generation", PrivacyTier.INTIMATE)

    assert resolved.provider == "ollama"
    assert isinstance(provider, OllamaProvider)
    assert spy.destinations == []
    assert "off-device egress refused" in caplog.text


def test_a_pool_default_is_never_the_rescue_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With ``default`` on the pool there is no local backend, so it refuses."""
    spy = SocketSpy(monkeypatch)
    router = ModelRouter(
        LLMRoutingConfig(default=_pool(), generation=LLMConfig(provider="anthropic")),
    )

    with pytest.raises(IntimateRoutingError) as caught:
        router.resolve("generation", PrivacyTier.INTIMATE)

    assert caught.value.stage_provider == "anthropic"
    assert spy.destinations == []


def test_a_pool_stage_with_a_pool_default_refuses_rather_than_dialling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pool everywhere is a loud refusal, not a quiet egress."""
    spy = SocketSpy(monkeypatch)
    router = ModelRouter(LLMRoutingConfig(default=_pool(), generation=_pool()))

    with pytest.raises(IntimateRoutingError) as caught:
        router.resolve("generation", PrivacyTier.INTIMATE)

    assert caught.value.stage_provider == "pool"
    assert "off-device provider 'pool'" in str(caught.value)
    assert spy.destinations == []


def test_a_writing_desk_role_on_the_pool_is_gated_the_same_way(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every role inherits the chokepoint; none re-checks the tier itself."""
    spy = SocketSpy(monkeypatch)
    router = ModelRouter(
        LLMRoutingConfig(default=_local(), writing_desk={"voice_drafter": _pool()}),
    )

    assert router.resolve_role("voice_drafter").provider == "pool"
    assert (
        router.resolve_role("voice_drafter", PrivacyTier.INTIMATE).provider == "ollama"
    )
    assert spy.destinations == []


@pytest.mark.parametrize(
    "tier", [PrivacyTier.OPEN, PrivacyTier.PERSONAL, PrivacyTier.UNCLASSIFIED, None]
)
def test_every_other_tier_still_routes_to_the_pool(
    tier: PrivacyTier | None,
) -> None:
    """The gate is INTIMATE-only; OPEN and PERSONAL are what the pool is for."""
    router = ModelRouter(LLMRoutingConfig(default=_local(), generation=_pool()))

    resolved = router.resolve("generation", tier)

    assert resolved.provider == "pool"
    assert isinstance(router.provider_for("generation", tier), PoolProvider)


def test_the_cloud_refusal_message_is_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Widening the gate must not reword the existing cloud refusal."""
    spy = SocketSpy(monkeypatch)
    router = ModelRouter(
        LLMRoutingConfig(
            default=LLMConfig(provider="anthropic"),
            generation=LLMConfig(provider="openai"),
        ),
    )

    with pytest.raises(IntimateRoutingError) as caught:
        router.resolve("generation", PrivacyTier.INTIMATE)

    assert "cannot route to cloud provider 'openai'" in str(caught.value)
    assert spy.destinations == []
