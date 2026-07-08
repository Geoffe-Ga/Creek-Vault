"""Writing Desk makes live LLM calls via a router-built client (#658).

Wires the gap left after #649: the conductor stored an ``llm_client`` but built
a bare ``VoiceAgent()`` stub, and neither the CLI nor MCP built a real client.
These tests pin: the client now reaches the VoiceAgent, ``run_author`` threads
it, and ``AuthorLLMClient.for_voice_or_none`` routes the voice role through the
chokepoint and degrades to ``None`` (deterministic stub) when the provider is
unavailable.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import MagicMock

from creek.author.client import AuthorLLMClient
from creek.author.conductor import build_default_conductor, run_author
from creek.classify.llm.router import ModelRouter
from creek.config import AuthorConfig, LLMRoutingConfig

if TYPE_CHECKING:
    import pytest

    from creek.config import LLMConfig


class _FakeProvider:
    """A provider double whose ``available`` is controllable."""

    def __init__(self, *, available: bool = True) -> None:
        self._available = available

    @property
    def available(self) -> bool:
        return self._available

    def complete(self, prompt: str, **_kw: object) -> object:
        return type("C", (), {"text": f"voiced:{prompt}", "usage": None})()


# --- the seam now reaches the collaborator that calls the LLM --------------


def test_build_default_conductor_threads_client_to_voice() -> None:
    """The injected client reaches the VoiceAgent, not just the conductor."""
    client = AuthorLLMClient(MagicMock())
    conductor = build_default_conductor(max_rounds=1, llm_client=client)
    assert conductor.voice.llm_client is client
    assert conductor.llm_client is client


def test_build_default_conductor_none_keeps_stub() -> None:
    """Without a client the VoiceAgent stays deterministic (client None)."""
    conductor = build_default_conductor(max_rounds=1)
    assert conductor.voice.llm_client is None


def test_run_author_threads_client(monkeypatch: pytest.MonkeyPatch) -> None:
    """``run_author`` forwards its llm_client to build_default_conductor."""
    captured: dict[str, object] = {}
    fake_conductor = MagicMock()
    fake_conductor.run.return_value = MagicMock()

    def _fake_build(*, llm_client: object = None, **_kw: object) -> object:
        captured["llm_client"] = llm_client
        return fake_conductor

    monkeypatch.setattr("creek.author.conductor.build_default_conductor", _fake_build)
    monkeypatch.setattr(
        "creek.author.conductor.load_medium_contract",
        lambda _m, _v: None,
    )
    monkeypatch.setattr("creek.author.conductor._enforce_chat_ceiling", lambda d: d)

    client = AuthorLLMClient(MagicMock())
    run_author(medium="research", query="q", vault=MagicMock(), llm_client=client)
    assert captured["llm_client"] is client


# --- for_voice_or_none: routing + graceful availability --------------------


def _capture_build(
    monkeypatch: pytest.MonkeyPatch,
    *,
    available: bool,
) -> dict[str, LLMConfig]:
    captured: dict[str, LLMConfig] = {}

    def _fake_build(cfg: LLMConfig) -> _FakeProvider:
        captured["cfg"] = cfg
        return _FakeProvider(available=available)

    monkeypatch.setattr("creek.author.client.build_provider", _fake_build)
    return captured


def test_for_voice_or_none_returns_client_when_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An available provider yields a usable client."""
    _capture_build(monkeypatch, available=True)
    router = ModelRouter(
        LLMRoutingConfig.model_validate({"generation": {"provider": "anthropic"}}),
    )
    client = AuthorLLMClient.for_voice_or_none(router)
    assert client is not None
    assert client.available


def test_for_voice_or_none_returns_none_when_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unavailable provider degrades to None (deterministic stub)."""
    _capture_build(monkeypatch, available=False)
    router = ModelRouter(
        LLMRoutingConfig.model_validate({"generation": {"provider": "anthropic"}}),
    )
    assert AuthorLLMClient.for_voice_or_none(router) is None


def test_for_voice_or_none_uses_writing_desk_role_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two-backend config: the voice client uses the configured role provider."""
    captured = _capture_build(monkeypatch, available=True)
    router = ModelRouter(
        LLMRoutingConfig.model_validate(
            {
                "default": {"provider": "ollama"},
                "writing_desk": {"voice_drafter": {"provider": "anthropic"}},
            },
        ),
    )
    AuthorLLMClient.for_voice_or_none(router, author=AuthorConfig())
    assert captured["cfg"].provider == "anthropic"


def test_for_voice_or_none_intimate_stays_local(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An Intimate tier redirects the voice role to the local default."""
    from creek.models import PrivacyTier

    captured = _capture_build(monkeypatch, available=True)
    router = ModelRouter(
        LLMRoutingConfig.model_validate(
            {
                "default": {"provider": "ollama"},
                "writing_desk": {"voice_drafter": {"provider": "anthropic"}},
            },
        ),
    )
    AuthorLLMClient.for_voice_or_none(router, tier=PrivacyTier.INTIMATE)
    assert captured["cfg"].provider == "ollama"  # redirected off the cloud role


def test_available_delegates_to_provider() -> None:
    """``AuthorLLMClient.available`` reflects the wrapped provider."""
    assert AuthorLLMClient(_FakeProvider(available=True)).available
    assert not AuthorLLMClient(_FakeProvider(available=False)).available
