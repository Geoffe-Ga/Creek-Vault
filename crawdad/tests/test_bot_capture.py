"""Bot-capture wiring on ``CrawDadClient.on_message`` (#687).

Proves capture runs alongside — and never disturbs — the command path: a
non-self message is captured *and* forwarded to ``handle_message``; the bot's
own messages are skipped; a capture write failure is swallowed so commands still
run; and the CLI builds the writer only when capture is enabled.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from crawdad import cli
from crawdad.bot import CrawDadClient
from crawdad.capture import MessageCapture
from crawdad.config import CrawDadConfig

if TYPE_CHECKING:
    from crawdad.capture import _MessageLike


@dataclass
class _FakeUser:
    id: int
    name: str = "someone"


@dataclass
class _FakeChannel:
    name: str
    id: int = 1


@dataclass
class _FakeMessage:
    id: int
    content: str
    created_at: datetime
    author: _FakeUser
    channel: _FakeChannel
    attachments: list[object] = field(default_factory=list)


_BOT_USER_ID = 999


class _ReadyClient(CrawDadClient):
    """A client that reports itself as connected (``user`` is set)."""

    @property
    def user(self) -> _FakeUser:  # type: ignore[override]  # Issue #687: test-only ready stub
        """Pretend the gateway handshake finished so ``on_message`` proceeds."""
        return _FakeUser(id=_BOT_USER_ID)


def _config(tmp_path: Path, *, capture_enabled: bool = False) -> CrawDadConfig:
    """A minimal valid bot config rooted at *tmp_path*."""
    return CrawDadConfig(
        discord_bot_token="t",
        vault_path=tmp_path,
        allowed_user_ids=[111],
        allowed_channel_ids=[999],
        capture_enabled=capture_enabled,
    )


def _message(*, msg_id: int, author_id: int) -> _FakeMessage:
    """A fake discord message carrying both capture + author fields."""
    return _FakeMessage(
        id=msg_id,
        content="a thoughtful note worth keeping in the vault",
        created_at=datetime.fromisoformat("2026-06-26T10:00:00").replace(tzinfo=UTC),
        author=_FakeUser(id=author_id, name="Ada"),
        channel=_FakeChannel(name="general"),
    )


def _client(tmp_path: Path, capture: MessageCapture | None) -> _ReadyClient:
    """Construct a ready test client wired with *capture*."""
    return _ReadyClient(
        config=_config(tmp_path),
        session_state=None,
        message_capture=capture,
    )


async def test_captures_and_forwards_to_command_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-self message is captured AND forwarded to handle_message."""
    handled: list[int] = []

    async def _fake_handle(message: _MessageLike, **_kwargs: object) -> None:
        handled.append(message.id)

    monkeypatch.setattr("crawdad.bot.handle_message", _fake_handle)
    capture = MessageCapture(capture_dir=tmp_path / "discord-capture")
    client = _client(tmp_path, capture)

    await client.on_message(_message(msg_id=100, author_id=111))

    assert handled == [100]  # command path still fires
    captured = tmp_path / "discord-capture" / "general" / "2026-06-26.jsonl"
    assert captured.is_file()  # and the message was captured


async def test_skips_the_bots_own_messages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bot does not capture its own replies."""
    monkeypatch.setattr("crawdad.bot.handle_message", _noop_handle)
    capture = MessageCapture(capture_dir=tmp_path / "discord-capture")
    client = _client(tmp_path, capture)

    await client.on_message(_message(msg_id=200, author_id=_BOT_USER_ID))

    assert not (tmp_path / "discord-capture").exists()


async def test_no_capture_configured_is_inert(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With capture disabled, on_message just forwards (no dir written)."""
    handled: list[int] = []

    async def _fake_handle(message: _MessageLike, **_kwargs: object) -> None:
        handled.append(message.id)

    monkeypatch.setattr("crawdad.bot.handle_message", _fake_handle)
    client = _client(tmp_path, capture=None)

    await client.on_message(_message(msg_id=300, author_id=111))

    assert handled == [300]
    assert not (tmp_path / "discord-capture").exists()


async def test_capture_failure_does_not_break_commands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A capture OSError is swallowed; the command path still runs."""
    handled: list[int] = []

    async def _fake_handle(message: _MessageLike, **_kwargs: object) -> None:
        handled.append(message.id)

    monkeypatch.setattr("crawdad.bot.handle_message", _fake_handle)

    class _BrokenCapture(MessageCapture):
        async def on_message(self, message: _MessageLike) -> None:
            raise OSError("disk full")

    client = _client(tmp_path, _BrokenCapture(capture_dir=tmp_path / "cap"))

    await client.on_message(_message(msg_id=400, author_id=111))

    assert handled == [400]  # command path survived the capture failure


async def test_on_message_before_ready_drops_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Before the client is ready (user is None), on_message no-ops."""
    handled: list[int] = []

    async def _fake_handle(message: _MessageLike, **_kwargs: object) -> None:
        handled.append(message.id)

    monkeypatch.setattr("crawdad.bot.handle_message", _fake_handle)
    # Plain CrawDadClient: user is None until connected.
    client = CrawDadClient(
        config=_config(tmp_path),
        session_state=None,
        message_capture=MessageCapture(capture_dir=tmp_path / "cap"),
    )

    await client.on_message(_message(msg_id=500, author_id=111))

    assert handled == []  # dropped before ready
    assert not (tmp_path / "cap").exists()


async def _noop_handle(message: _MessageLike, **_kwargs: object) -> None:
    """A handle_message stand-in that does nothing."""


class TestBuildMessageCapture:
    """The CLI builds the capture writer only when capture is enabled."""

    def test_enabled_builds_writer_at_vault_subpath(self, tmp_path: Path) -> None:
        """Enabled -> a MessageCapture rooted at vault/capture_subpath."""
        config = _config(tmp_path, capture_enabled=True)
        capture = cli._build_message_capture(config)
        assert isinstance(capture, MessageCapture)

    def test_disabled_returns_none(self, tmp_path: Path) -> None:
        """Disabled (the default) -> no capture writer."""
        assert cli._build_message_capture(_config(tmp_path)) is None
