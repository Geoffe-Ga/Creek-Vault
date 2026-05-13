"""Tests for ``crawdad.bot`` — allowlist, replies, subprocess resilience."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from crawdad.bot import handle_message, render_state_unavailable_reply
from crawdad.config import CrawDadConfig
from crawdad.state import SessionState, StateUnavailableError


@dataclass
class _FakeAuthor:
    id: int
    bot: bool = False


@dataclass
class _FakeChannel:
    id: int
    sent: list[str]

    async def send(self, content: str) -> None:
        self.sent.append(content)


@dataclass
class _FakeMessage:
    author: _FakeAuthor
    channel: _FakeChannel
    content: str


@pytest.fixture
def config(tmp_path: Path) -> CrawDadConfig:
    return CrawDadConfig(
        discord_bot_token="t",
        anthropic_api_key="k",
        vault_path=tmp_path,
        allowed_user_ids=[111],
        allowed_channel_ids=[999],
    )


@pytest.fixture
def session_state() -> SessionState:
    return SessionState(
        raw_markdown="snapshot",
        wavelength_snapshot="rising / medicine",
        eddies=("eddy-clarity",),
        threads=("thread-voice",),
        suggested_questions=("What is surfacing?",),
    )


async def test_handle_message_allowlisted_user_gets_stub_reply(
    config: CrawDadConfig, session_state: SessionState
) -> None:
    """A DM from the allowlisted user/channel pair receives the stub reply."""
    channel = _FakeChannel(id=999, sent=[])
    message = _FakeMessage(
        author=_FakeAuthor(id=111),
        channel=channel,
        content="hello",
    )

    await handle_message(
        message,
        config=config,
        session_state=session_state,
        bot_user_id=42,
    )

    assert len(channel.sent) == 1
    assert "crawdad" in channel.sent[0].lower()


async def test_handle_message_ignores_non_allowlisted_user(
    config: CrawDadConfig, session_state: SessionState
) -> None:
    """Non-allowlisted users get *no* reply (silent ignore, per FEAT-013)."""
    channel = _FakeChannel(id=999, sent=[])
    message = _FakeMessage(
        author=_FakeAuthor(id=222),  # not allowlisted
        channel=channel,
        content="hello",
    )

    await handle_message(
        message,
        config=config,
        session_state=session_state,
        bot_user_id=42,
    )

    assert channel.sent == []


async def test_handle_message_ignores_non_allowlisted_channel(
    config: CrawDadConfig, session_state: SessionState
) -> None:
    """Allowlisted user in a non-allowlisted channel is also a silent ignore."""
    channel = _FakeChannel(id=888, sent=[])
    message = _FakeMessage(
        author=_FakeAuthor(id=111),
        channel=channel,
        content="hello",
    )

    await handle_message(
        message,
        config=config,
        session_state=session_state,
        bot_user_id=42,
    )

    assert channel.sent == []


async def test_handle_message_ignores_self(
    config: CrawDadConfig, session_state: SessionState
) -> None:
    """The bot does not respond to its own messages — prevents echo loops."""
    channel = _FakeChannel(id=999, sent=[])
    message = _FakeMessage(
        author=_FakeAuthor(id=42, bot=True),
        channel=channel,
        content="hello",
    )

    await handle_message(
        message,
        config=config,
        session_state=session_state,
        bot_user_id=42,
    )

    assert channel.sent == []


def test_render_state_unavailable_reply_directs_to_creek_state() -> None:
    """The user-facing missing-state message tells the user what to run."""
    reply = render_state_unavailable_reply(StateUnavailableError("missing"))

    assert "creek state" in reply
    assert "no audit report" in reply.lower()


async def test_handle_message_uses_state_unavailable_reply(
    config: CrawDadConfig,
) -> None:
    """When session_state is None the allowlisted user gets the guidance reply."""
    channel = _FakeChannel(id=999, sent=[])
    message = _FakeMessage(
        author=_FakeAuthor(id=111),
        channel=channel,
        content="hi",
    )

    await handle_message(
        message,
        config=config,
        session_state=None,
        bot_user_id=42,
    )

    assert len(channel.sent) == 1
    assert "creek state" in channel.sent[0]


async def test_handle_subprocess_unavailable_replies_gracefully(
    config: CrawDadConfig,
) -> None:
    """A simulated MCP subprocess failure produces the documented soft error."""
    from crawdad.bot import render_mcp_unavailable_reply
    from crawdad.mcp_client import MCPUnavailableError

    reply = render_mcp_unavailable_reply(MCPUnavailableError("subprocess died"))

    assert "creek-tools" in reply
    assert "unreachable" in reply.lower()


@pytest.mark.parametrize(
    ("user_id", "channel_id", "expected_sent"),
    [
        (111, 999, 1),
        (111, 888, 0),
        (222, 999, 0),
    ],
)
async def test_handle_message_parametrized(
    config: CrawDadConfig,
    session_state: SessionState,
    user_id: int,
    channel_id: int,
    expected_sent: int,
) -> None:
    """Allowlist gates the response across user/channel combinations."""
    channel = _FakeChannel(id=channel_id, sent=[])
    message: Any = _FakeMessage(
        author=_FakeAuthor(id=user_id),
        channel=channel,
        content="hi",
    )

    await handle_message(
        message,
        config=config,
        session_state=session_state,
        bot_user_id=42,
    )

    assert len(channel.sent) == expected_sent


def test_crawdad_client_init_sets_intents(
    config: CrawDadConfig, session_state: SessionState
) -> None:
    """The subclass requests ``message_content`` so DMs are readable."""
    from crawdad.bot import CrawDadClient

    client = CrawDadClient(config=config, session_state=session_state)

    assert client.intents.message_content is True


async def test_crawdad_client_on_message_delegates(
    config: CrawDadConfig,
    session_state: SessionState,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``on_message`` forwards into :func:`handle_message`."""
    from crawdad.bot import CrawDadClient

    seen: dict[str, Any] = {}

    async def _spy(message: Any, **kwargs: Any) -> None:
        seen["message"] = message
        seen["kwargs"] = kwargs

    monkeypatch.setattr("crawdad.bot.handle_message", _spy)

    client = CrawDadClient(config=config, session_state=session_state)

    class _User:
        id = 7

    object.__setattr__(client, "_connection", None)  # avoid real ws state
    monkeypatch.setattr(
        type(client),
        "user",
        property(lambda _self: _User()),
    )

    channel = _FakeChannel(id=999, sent=[])
    message: Any = _FakeMessage(
        author=_FakeAuthor(id=111),
        channel=channel,
        content="hi",
    )

    await client.on_message(message)

    assert seen["message"] is message
    assert seen["kwargs"]["bot_user_id"] == 7


async def test_crawdad_client_on_message_skips_when_not_ready(
    config: CrawDadConfig,
    session_state: SessionState,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If ``self.user`` is None, the handler is skipped (not crashed)."""
    from crawdad.bot import CrawDadClient

    called = False

    async def _spy(*_args: Any, **_kwargs: Any) -> None:
        nonlocal called
        called = True

    monkeypatch.setattr("crawdad.bot.handle_message", _spy)

    client = CrawDadClient(config=config, session_state=session_state)
    monkeypatch.setattr(
        type(client),
        "user",
        property(lambda _self: None),
    )

    channel = _FakeChannel(id=999, sent=[])
    await client.on_message(
        _FakeMessage(  # type: ignore[arg-type]
            author=_FakeAuthor(id=111),
            channel=channel,
            content="hi",
        )
    )

    assert called is False


async def test_crawdad_client_on_ready_logs(
    config: CrawDadConfig,
    session_state: SessionState,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``on_ready`` logs the connected user identity for the operator."""
    from crawdad.bot import CrawDadClient

    client = CrawDadClient(config=config, session_state=session_state)
    monkeypatch.setattr(
        type(client),
        "user",
        property(lambda _self: "crawdad-test"),
    )
    caplog.set_level("INFO", logger="crawdad.bot")

    await client.on_ready()

    assert any("connected" in record.message for record in caplog.records)
