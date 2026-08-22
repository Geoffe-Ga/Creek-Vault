"""Bot-capture wiring and its trust boundary (#687, #1052).

Two concerns live here.

The #687 half proves capture runs alongside — and never disturbs — the command
path: a non-self message is captured *and* forwarded to ``handle_message``; the
bot's own messages are skipped; a capture write failure is swallowed so commands
still run; and the CLI builds the writer only when capture is enabled.

The #1052 half pins the *boundary*. Capture used to run before
``handle_message``, so it bypassed both allowlists, the ``author.bot`` filter,
and ``channel_privacy_tiers`` entirely — a non-allowlisted user in a
non-allowlisted ``intimate`` channel still landed in
``<vault>/discord-capture/``, untiered, which reads downstream as
``unclassified`` and therefore ranks with ``personal``. That is a privacy
DE-escalation. Capture must now clear the same gate the command path clears
*plus* a tier gate: only ``open`` and ``personal`` channels are admitted,
because a capture record carries no tier of its own.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from crawdad import cli
from crawdad.bot import CrawDadClient, _stub_reply
from crawdad.capture import MessageCapture
from crawdad.config import AttachmentConfig, CrawDadConfig

if TYPE_CHECKING:
    from crawdad.capture import _MessageLike

# Five distinct ids so no assertion can pass by collision. The pre-#1052
# fixture set ``allowed_channel_ids=[999]`` while the fake channel defaulted to
# ``id=1`` and ``999`` was also the bot's own user id — an arrangement in which
# "the allowlisted channel was captured" could never actually be observed.
_ALLOWED_USER_ID = 111
_OTHER_USER_ID = 112
_ALLOWED_CHANNEL_ID = 555
_OTHER_CHANNEL_ID = 556
_BOT_USER_ID = 999

_CAPTURE_DIRNAME = "discord-capture"
_CAPTURED_JSONL = Path(_CAPTURE_DIRNAME) / "general" / "2026-06-26.jsonl"


@dataclass
class _FakeUser:
    id: int
    name: str = "someone"
    bot: bool = False


class _ExplodingAuthor:
    """An author whose ``bot`` flag raises when the capture gate reads it.

    Used to prove the gate is evaluated *inside* ``_capture_message``'s
    ``try/except`` — a gate that blows up must refuse the capture, not take
    the command path down with it.
    """

    def __init__(self, user_id: int) -> None:
        """Record the author id; ``name`` mirrors the plain fake user."""
        self.id = user_id
        self.name = "Ada"

    @property
    def bot(self) -> bool:
        """Raise instead of answering, simulating a gate-time failure."""
        msg = "author.bot lookup exploded"
        raise RuntimeError(msg)


@dataclass
class _FakeChannel:
    name: str
    id: int = _ALLOWED_CHANNEL_ID
    sent: list[str] = field(default_factory=list)

    async def send(self, content: str) -> None:
        """Record a reply so tests can observe the command path answering."""
        self.sent.append(content)


@dataclass
class _FakeMessage:
    id: int
    content: str
    created_at: datetime
    author: _FakeUser | _ExplodingAuthor
    channel: _FakeChannel
    attachments: list[object] = field(default_factory=list)


class _ReadyClient(CrawDadClient):
    """A client that reports itself as connected (``user`` is set)."""

    @property
    def user(self) -> _FakeUser:  # type: ignore[override]  # Issue #687: test-only ready stub
        """Pretend the gateway handshake finished so ``on_message`` proceeds."""
        return _FakeUser(id=_BOT_USER_ID)


def _config(
    tmp_path: Path,
    *,
    capture_enabled: bool = False,
    channel_tiers: dict[int, str] | None = None,
) -> CrawDadConfig:
    """A minimal valid bot config rooted at *tmp_path*.

    *channel_tiers* populates ``attachments.channel_privacy_tiers`` so a test
    can declare a channel ``intimate`` / ``all`` and watch capture refuse it.
    """
    return CrawDadConfig(
        discord_bot_token="t",
        vault_path=tmp_path,
        allowed_user_ids=[_ALLOWED_USER_ID],
        allowed_channel_ids=[_ALLOWED_CHANNEL_ID],
        capture_enabled=capture_enabled,
        attachments=AttachmentConfig(channel_privacy_tiers=channel_tiers or {}),
    )


def _message(
    *,
    msg_id: int,
    author_id: int,
    channel_id: int = _ALLOWED_CHANNEL_ID,
    author_bot: bool = False,
) -> _FakeMessage:
    """A fake discord message carrying both capture + author fields.

    *channel_id* defaults to the allowlisted channel so a test only has to
    name it when the channel is the variable under test.
    """
    return _FakeMessage(
        id=msg_id,
        content="a thoughtful note worth keeping in the vault",
        created_at=datetime.fromisoformat("2026-06-26T10:00:00").replace(tzinfo=UTC),
        author=_FakeUser(id=author_id, name="Ada", bot=author_bot),
        channel=_FakeChannel(name="general", id=channel_id),
    )


def _exploding_message(*, msg_id: int) -> _FakeMessage:
    """A message whose author raises the moment the gate inspects ``bot``."""
    return _FakeMessage(
        id=msg_id,
        content="a thoughtful note worth keeping in the vault",
        created_at=datetime.fromisoformat("2026-06-26T10:00:00").replace(tzinfo=UTC),
        author=_ExplodingAuthor(_ALLOWED_USER_ID),
        channel=_FakeChannel(name="general", id=_ALLOWED_CHANNEL_ID),
    )


def _client(
    tmp_path: Path,
    capture: MessageCapture | None,
    *,
    channel_tiers: dict[int, str] | None = None,
) -> _ReadyClient:
    """Construct a ready test client wired with *capture*."""
    return _ReadyClient(
        config=_config(tmp_path, channel_tiers=channel_tiers),
        session_state=None,
        message_capture=capture,
    )


async def test_captures_and_forwards_to_command_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-self message is captured AND forwarded to handle_message.

    Not a #1052 RED test — it is the #687 test repaired. It previously sent an
    allowlisted user into a *non*-allowlisted channel, so its "the message was
    captured" assertion documented the bypass rather than the feature. Pointing
    it at ``_ALLOWED_CHANNEL_ID`` makes the assertion mean what it claims, and
    it must stay green through the fix (the fix is strictly narrowing).
    """
    handled: list[int] = []

    async def _fake_handle(message: _MessageLike, **_kwargs: object) -> None:
        handled.append(message.id)

    monkeypatch.setattr("crawdad.bot.handle_message", _fake_handle)
    capture = MessageCapture(capture_dir=tmp_path / _CAPTURE_DIRNAME)
    client = _client(tmp_path, capture)

    message = _message(
        msg_id=100, author_id=_ALLOWED_USER_ID, channel_id=_ALLOWED_CHANNEL_ID
    )

    await client.on_message(message)

    assert handled == [100]  # command path still fires
    assert (tmp_path / _CAPTURED_JSONL).is_file()  # and the message was captured


async def test_skips_the_bots_own_messages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bot does not capture its own replies."""
    monkeypatch.setattr("crawdad.bot.handle_message", _noop_handle)
    capture = MessageCapture(capture_dir=tmp_path / _CAPTURE_DIRNAME)
    client = _client(tmp_path, capture)

    await client.on_message(_message(msg_id=200, author_id=_BOT_USER_ID))

    assert not (tmp_path / _CAPTURE_DIRNAME).exists()


async def test_no_capture_configured_is_inert(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With capture disabled, on_message just forwards (no dir written)."""
    handled: list[int] = []

    async def _fake_handle(message: _MessageLike, **_kwargs: object) -> None:
        handled.append(message.id)

    monkeypatch.setattr("crawdad.bot.handle_message", _fake_handle)
    client = _client(tmp_path, capture=None)

    await client.on_message(_message(msg_id=300, author_id=_ALLOWED_USER_ID))

    assert handled == [300]
    assert not (tmp_path / _CAPTURE_DIRNAME).exists()


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

    await client.on_message(_message(msg_id=400, author_id=_ALLOWED_USER_ID))

    assert handled == [400]  # command path survived the capture failure


async def test_non_oserror_capture_failure_is_swallowed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-OSError (e.g. a bad message shape) also never breaks commands."""
    handled: list[int] = []

    async def _fake_handle(message: _MessageLike, **_kwargs: object) -> None:
        handled.append(message.id)

    monkeypatch.setattr("crawdad.bot.handle_message", _fake_handle)

    class _BrokenCapture(MessageCapture):
        async def on_message(self, message: _MessageLike) -> None:
            raise ValueError("unexpected message shape")

    client = _client(tmp_path, _BrokenCapture(capture_dir=tmp_path / "cap"))

    await client.on_message(_message(msg_id=401, author_id=_ALLOWED_USER_ID))

    assert handled == [401]  # the broadened catch kept the command path alive


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

    await client.on_message(_message(msg_id=500, author_id=_ALLOWED_USER_ID))

    assert handled == []  # dropped before ready
    assert not (tmp_path / "cap").exists()


async def _noop_handle(message: _MessageLike, **_kwargs: object) -> None:
    """A handle_message stand-in that does nothing."""


# ---------------------------------------------------------------------------
# #1052 — capture must clear the command path's gate, plus a tier gate
# ---------------------------------------------------------------------------


async def test_non_allowlisted_channel_is_not_captured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An allowlisted user speaking in an un-allowlisted channel is not captured.

    ``config.is_allowed`` refuses the command path here; capture must refuse
    for the same reason. Anything else means every channel the bot is merely
    *present* in gets logged into the vault.
    """
    monkeypatch.setattr("crawdad.bot.handle_message", _noop_handle)
    capture = MessageCapture(capture_dir=tmp_path / _CAPTURE_DIRNAME)
    client = _client(tmp_path, capture)

    message = _message(
        msg_id=600, author_id=_ALLOWED_USER_ID, channel_id=_OTHER_CHANNEL_ID
    )

    await client.on_message(message)

    assert not (tmp_path / _CAPTURE_DIRNAME).exists()


async def test_non_allowlisted_user_is_not_captured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stranger in an allowlisted channel is not captured.

    The user allowlist is the bot's only consent record. A person who never
    agreed to be logged must not be written into someone else's vault.
    """
    monkeypatch.setattr("crawdad.bot.handle_message", _noop_handle)
    capture = MessageCapture(capture_dir=tmp_path / _CAPTURE_DIRNAME)
    client = _client(tmp_path, capture)

    message = _message(
        msg_id=601, author_id=_OTHER_USER_ID, channel_id=_ALLOWED_CHANNEL_ID
    )

    await client.on_message(message)

    assert not (tmp_path / _CAPTURE_DIRNAME).exists()


async def test_other_bots_messages_are_not_captured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Another bot / webhook is refused on the ``author.bot`` flag alone.

    Distinct from ``test_skips_the_bots_own_messages``: this author is *not*
    CrawDad (the self-id check passes it through) and satisfies BOTH
    allowlists, so the only gate left that can refuse it is ``author.bot``.
    Machine chatter is not the operator's thinking and must not be ingested
    as if it were.
    """
    monkeypatch.setattr("crawdad.bot.handle_message", _noop_handle)
    capture = MessageCapture(capture_dir=tmp_path / _CAPTURE_DIRNAME)
    client = _client(tmp_path, capture)

    message = _message(
        msg_id=602,
        author_id=_ALLOWED_USER_ID,
        channel_id=_ALLOWED_CHANNEL_ID,
        author_bot=True,
    )

    await client.on_message(message)

    assert not (tmp_path / _CAPTURE_DIRNAME).exists()


async def test_intimate_channel_is_not_captured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A channel the operator declared ``intimate`` is never captured.

    This asserts the bypass is closed under the NARROWEST ceiling, not merely
    under the default one. Every other gate passes — allowlisted user,
    allowlisted channel, human author, not the bot itself — so only the tier
    gate can refuse. A capture record carries no tier field, so an intimate
    message written into ``discord-capture/`` arrives downstream as
    ``unclassified``, which ranks with ``personal`` (#961): a silent privacy
    de-escalation of the operator's most sensitive channel. Refusal is the
    only safe answer until the capture record can carry its ceiling.
    """
    monkeypatch.setattr("crawdad.bot.handle_message", _noop_handle)
    capture = MessageCapture(capture_dir=tmp_path / _CAPTURE_DIRNAME)
    tiers = {_ALLOWED_CHANNEL_ID: "intimate"}
    client = _client(tmp_path, capture, channel_tiers=tiers)
    message = _message(
        msg_id=603, author_id=_ALLOWED_USER_ID, channel_id=_ALLOWED_CHANNEL_ID
    )

    await client.on_message(message)

    assert not (tmp_path / _CAPTURE_DIRNAME).exists()


async def test_all_tier_channel_is_not_captured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A channel declared ``all`` is refused too — ``all`` admits intimate.

    ``all`` is the widest ceiling in the vocabulary, which by definition
    includes intimate content. Treating it as admissible would reopen the
    exact hole ``intimate`` closes.
    """
    monkeypatch.setattr("crawdad.bot.handle_message", _noop_handle)
    capture = MessageCapture(capture_dir=tmp_path / _CAPTURE_DIRNAME)
    tiers = {_ALLOWED_CHANNEL_ID: "all"}
    client = _client(tmp_path, capture, channel_tiers=tiers)
    message = _message(
        msg_id=604, author_id=_ALLOWED_USER_ID, channel_id=_ALLOWED_CHANNEL_ID
    )

    await client.on_message(message)

    assert not (tmp_path / _CAPTURE_DIRNAME).exists()


@pytest.mark.parametrize(
    ("tier", "msg_id"),
    [("open", 605), ("personal", 606), (None, 607)],
)
async def test_admitted_tier_channels_are_captured(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tier: str | None,
    msg_id: int,
) -> None:
    """``open`` / ``personal`` / unset channels keep being captured.

    LOAD-BEARING OVER-NARROWING GUARD — do not delete. The five refusal tests
    above are all satisfied by a "fix" that simply refuses everything, which
    would silently kill the whole #687 capture feature; none of them fails in
    that case. Only three tests do: this one,
    ``test_captures_and_forwards_to_command_path`` and
    ``test_end_to_end_allowlisted_channel_captures_and_replies`` — so none of
    the three is redundant with the others. This one is the only one that
    covers the tier axis. The unset case matters most: ``_channel_tier``
    defaults a missing channel to ``DEFAULT_CHANNEL_TIER`` (``personal``), so
    an operator who never wrote a ``channel_privacy_tiers`` block must not
    lose capture.
    """
    monkeypatch.setattr("crawdad.bot.handle_message", _noop_handle)
    capture = MessageCapture(capture_dir=tmp_path / _CAPTURE_DIRNAME)
    tiers = None if tier is None else {_ALLOWED_CHANNEL_ID: tier}
    client = _client(tmp_path, capture, channel_tiers=tiers)
    message = _message(
        msg_id=msg_id, author_id=_ALLOWED_USER_ID, channel_id=_ALLOWED_CHANNEL_ID
    )

    await client.on_message(message)

    captured = tmp_path / _CAPTURED_JSONL
    assert captured.is_file()
    assert f'"id": "{msg_id}"' in captured.read_text(encoding="utf-8")


async def test_gate_failure_refuses_capture_and_keeps_commands_alive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A gate that raises refuses the capture without breaking commands.

    Pins that the gate is evaluated INSIDE ``_capture_message``'s existing
    ``try/except Exception``. Two properties at once: fail-closed (an
    exception must never be read as "admitted"), and best-effort (the #687
    contract that capture can never take the command path down).
    """
    handled: list[int] = []

    async def _fake_handle(message: _MessageLike, **_kwargs: object) -> None:
        handled.append(message.id)

    monkeypatch.setattr("crawdad.bot.handle_message", _fake_handle)
    capture = MessageCapture(capture_dir=tmp_path / _CAPTURE_DIRNAME)
    client = _client(tmp_path, capture)

    await client.on_message(_exploding_message(msg_id=608))

    assert not (tmp_path / _CAPTURE_DIRNAME).exists()  # failed closed
    assert handled == [608]  # command path untouched


class TestEndToEndCaptureBoundary:
    """Capture and the command path answer to ONE boundary (#1052).

    These deliberately do not monkeypatch ``crawdad.bot.handle_message``: the
    real handler runs, so the allowlist decision under test is the same object
    the production command path consults. With ``router`` / ``composer`` /
    ``mcp_client`` all ``None`` and no pending batches, an admitted message
    gets ``_stub_reply()`` on ``message.channel.send`` — an observable proxy
    for "the command path ran".
    """

    async def test_end_to_end_allowlisted_channel_captures_and_replies(
        self, tmp_path: Path
    ) -> None:
        """The sanctioned path: the bot answers AND the message is captured."""
        capture = MessageCapture(capture_dir=tmp_path / _CAPTURE_DIRNAME)
        client = _client(tmp_path, capture)
        message = _message(
            msg_id=700,
            author_id=_ALLOWED_USER_ID,
            channel_id=_ALLOWED_CHANNEL_ID,
        )

        await client.on_message(message)

        assert message.channel.sent == [_stub_reply()]
        assert (tmp_path / _CAPTURED_JSONL).is_file()

    async def test_end_to_end_non_allowlisted_channel_is_silent_and_uncaptured(
        self, tmp_path: Path
    ) -> None:
        """Outside the allowlist the bot is fully inert: no reply, no record.

        FEAT-013 promises non-allowlisted callers get no response. Capture
        made that promise a half-truth — silent to the user, loud to the
        vault.
        """
        capture = MessageCapture(capture_dir=tmp_path / _CAPTURE_DIRNAME)
        client = _client(tmp_path, capture)
        message = _message(
            msg_id=701,
            author_id=_ALLOWED_USER_ID,
            channel_id=_OTHER_CHANNEL_ID,
        )

        await client.on_message(message)

        assert message.channel.sent == []
        assert not (tmp_path / _CAPTURE_DIRNAME).exists()

    async def test_end_to_end_intimate_channel_replies_but_does_not_capture(
        self, tmp_path: Path
    ) -> None:
        """The discriminating case: commands still answer, capture refuses.

        The tier gate is capture-scoped. ``channel_privacy_tiers`` has never
        gated whether the bot will *talk* to you in a channel, and this fix
        must not start: an operator who marks their journal channel
        ``intimate`` still expects ``/crawdad`` and free text to work there.
        This test fails if the new gate is bolted onto ``_passes_allowlist``
        (or into ``handle_message``) instead of onto the capture path.
        """
        capture = MessageCapture(capture_dir=tmp_path / _CAPTURE_DIRNAME)
        tiers = {_ALLOWED_CHANNEL_ID: "intimate"}
        client = _client(tmp_path, capture, channel_tiers=tiers)
        message = _message(
            msg_id=702,
            author_id=_ALLOWED_USER_ID,
            channel_id=_ALLOWED_CHANNEL_ID,
        )

        await client.on_message(message)

        assert message.channel.sent == [_stub_reply()]  # commands unaffected
        assert not (tmp_path / _CAPTURE_DIRNAME).exists()  # capture refused


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
