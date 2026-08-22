"""A Discord thread inherits its parent channel's declared privacy tier (#1265).

``message.channel.id`` inside a thread is the *thread's* id, so keying
``AttachmentConfig.channel_privacy_tiers`` on it alone silently drops the
ceiling the operator declared on the parent channel. These tests pin the
fixed contract at all three shipped call sites of
:func:`crawdad.bot._channel_tier` — the safety scan, the staged-batch
ceiling, and the #1052 bot-capture gate — plus the resolution rules the
helper itself owns.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from crawdad.bot import _capture_allowed, _channel_tier, handle_message
from crawdad.config import AttachmentConfig, CrawDadConfig
from crawdad.consent import PendingBatchStore
from crawdad.state import SessionState

_PARENT_ID = 999
_THREAD_ID = 1001
_USER_ID = 111
_BOT_ID = 42


@dataclass
class _FakeAuthor:
    """Subset of ``discord.Member`` the bot's gates read."""

    id: int
    bot: bool = False


@dataclass
class _FakeChannel:
    """A plain (non-thread) channel: it has no ``parent_id`` attribute."""

    id: int
    sent: list[str] = field(default_factory=list)

    async def send(self, content: str) -> None:
        """Record a reply so a test can observe the bot answering."""
        self.sent.append(content)


@dataclass
class _FakeThread(_FakeChannel):
    """A thread channel, mirroring ``discord.Thread.parent_id``.

    ``parent_id`` is ``None`` for the "parent cannot be determined" case,
    which must keep the fail-closed ``personal`` default.
    """

    parent_id: int | None = None


@dataclass
class _FakeAttachment:
    """Stand-in for ``discord.Attachment``."""

    filename: str = "note.md"
    size: int = 4
    payload: bytes = b"safe"
    url: str = "https://cdn.example/file"

    async def read(self) -> bytes:
        """Return the canned payload instead of hitting Discord's CDN."""
        return self.payload


@dataclass
class _FakeMessage:
    """Stand-in for ``discord.Message``."""

    author: _FakeAuthor
    channel: _FakeChannel
    content: str = ""
    id: int = 1
    attachments: list[Any] = field(default_factory=list)


class _CapturingSession:
    """MCP session stub that records every tool call's arguments."""

    def __init__(self) -> None:
        """Start with an empty call log."""
        self.calls: list[dict[str, Any]] = []

    async def call_tool(
        self, name: str, arguments: dict[str, Any] | None = None
    ) -> str:
        """Record *name* / *arguments* and answer with a benign scan body."""
        self.calls.append({"name": name, "args": arguments or {}})
        return "Scan summary: no findings"


class _StubMCPClient:
    """Stand-in for :class:`crawdad.mcp_client.MCPClient`."""

    def __init__(self, session: _CapturingSession) -> None:
        """Hold the single session every ``connect()`` yields."""
        self._session = session

    def connect(self) -> Any:
        """Yield the stub session from an async context manager."""
        from contextlib import asynccontextmanager

        session = self._session

        @asynccontextmanager
        async def _ctx() -> Any:
            yield session

        return _ctx()


def _config(tmp_path: Path, tiers: dict[int, str]) -> CrawDadConfig:
    """A config allowlisting the thread and declaring *tiers*."""
    return CrawDadConfig(
        discord_bot_token="t",
        vault_path=tmp_path,
        allowed_user_ids=[_USER_ID],
        allowed_channel_ids=[_PARENT_ID, _THREAD_ID],
        attachments=AttachmentConfig(channel_privacy_tiers=tiers),
    )


@pytest.fixture
def session_state() -> SessionState:
    """A minimal session state so the handler never hits the state path."""
    return SessionState(
        raw_markdown="snapshot",
        wavelength_snapshot="rising / medicine",
        eddies=("eddy-clarity",),
        threads=("thread-voice",),
        suggested_questions=("What is surfacing?",),
    )


async def _stage_in_thread(
    *,
    config: CrawDadConfig,
    session_state: SessionState,
    store: PendingBatchStore | None = None,
) -> _CapturingSession:
    """Drive one attachment turn posted inside the thread; return the session."""
    thread = _FakeThread(id=_THREAD_ID, parent_id=_PARENT_ID)
    message: Any = _FakeMessage(
        author=_FakeAuthor(id=_USER_ID),
        channel=thread,
        id=7,
        attachments=[_FakeAttachment()],
    )
    session = _CapturingSession()
    await handle_message(
        message,
        config=config,
        session_state=session_state,
        bot_user_id=_BOT_ID,
        mcp_client=_StubMCPClient(session),  # type: ignore[arg-type]
        known_tools=("creek.redact.scan",),
        pending_batches=store,
    )
    return session


async def test_safety_scan_ceiling_inherits_intimate_parent(
    tmp_path: Path, session_state: SessionState
) -> None:
    """Call site 1: ``creek.redact.scan`` gets the parent's declared ceiling.

    Before #1265 the thread id missed ``channel_privacy_tiers`` entirely and
    the scan ran at the ``personal`` default.
    """
    config = _config(tmp_path, {_PARENT_ID: "intimate"})

    session = await _stage_in_thread(config=config, session_state=session_state)

    scan = next(c for c in session.calls if c["name"] == "creek.redact.scan")
    assert scan["args"]["privacy_tier_ceiling"] == "intimate"


async def test_staged_batch_ceiling_inherits_intimate_parent(
    tmp_path: Path, session_state: SessionState
) -> None:
    """Call site 2: the pending batch records the parent's ceiling for ingest."""
    config = _config(tmp_path, {_PARENT_ID: "intimate"})
    store = PendingBatchStore(ttl_seconds=60.0, clock=lambda: 0.0)

    await _stage_in_thread(config=config, session_state=session_state, store=store)

    batch = store.get(_THREAD_ID)
    assert batch is not None
    assert batch.privacy_tier_ceiling == "intimate"


async def test_capture_gate_refuses_thread_inside_intimate_parent(
    tmp_path: Path,
) -> None:
    """Call site 3: bot-capture refuses a thread whose parent is ``intimate``.

    ``intimate`` is not in ``CAPTURE_ADMITTED_TIERS``; before #1265 the thread
    resolved to ``personal`` and capture wrote the message anyway.
    """
    config = _config(tmp_path, {_PARENT_ID: "intimate"})
    message: Any = _FakeMessage(
        author=_FakeAuthor(id=_USER_ID),
        channel=_FakeThread(id=_THREAD_ID, parent_id=_PARENT_ID),
    )

    assert not _capture_allowed(message, config=config, bot_user_id=_BOT_ID)


async def test_capture_gate_still_admits_thread_inside_open_parent(
    tmp_path: Path,
) -> None:
    """Over-narrowing guard: an ``open`` parent must not kill capture.

    Without this, "refuse every thread" satisfies every other test here while
    silently deleting the #1052 capture feature for threads.
    """
    config = _config(tmp_path, {_PARENT_ID: "open"})
    message: Any = _FakeMessage(
        author=_FakeAuthor(id=_USER_ID),
        channel=_FakeThread(id=_THREAD_ID, parent_id=_PARENT_ID),
    )

    assert _capture_allowed(message, config=config, bot_user_id=_BOT_ID)


@pytest.mark.parametrize(
    ("tiers", "expected"),
    [
        # The thread's own stricter declaration wins over an open parent.
        ({_PARENT_ID: "open", _THREAD_ID: "intimate"}, "intimate"),
        # ...and the parent's stricter declaration wins over an open thread.
        ({_PARENT_ID: "intimate", _THREAD_ID: "open"}, "intimate"),
        # Neither declared: the fail-closed default, never the parent's absence.
        ({}, "personal"),
        # Parent declared only.
        ({_PARENT_ID: "intimate"}, "intimate"),
        # Thread declared only — an undeclared parent contributes nothing.
        ({_THREAD_ID: "open"}, "open"),
        # ``all`` outranks ``personal`` in the one ordering table.
        ({_PARENT_ID: "all", _THREAD_ID: "personal"}, "all"),
    ],
)
def test_thread_resolves_to_most_restrictive_of_thread_and_parent(
    tmp_path: Path, tiers: dict[int, str], expected: str
) -> None:
    """The helper takes the most restrictive of the thread's and parent's tiers."""
    config = _config(tmp_path, tiers)
    thread = _FakeThread(id=_THREAD_ID, parent_id=_PARENT_ID)

    assert _channel_tier(channel=thread, config=config) == expected


@pytest.mark.parametrize(
    ("tiers", "expected"),
    [({_THREAD_ID: "intimate"}, "intimate"), ({_PARENT_ID: "intimate"}, "personal")],
)
def test_undeterminable_parent_keeps_the_personal_default(
    tmp_path: Path, tiers: dict[int, str], expected: str
) -> None:
    """A thread with no resolvable parent never widens past its own entry."""
    config = _config(tmp_path, tiers)
    thread = _FakeThread(id=_THREAD_ID, parent_id=None)

    assert _channel_tier(channel=thread, config=config) == expected


@pytest.mark.parametrize(
    ("tiers", "expected"),
    [
        ({_PARENT_ID: "intimate"}, "intimate"),
        ({_PARENT_ID: "open"}, "open"),
        ({}, "personal"),
    ],
)
def test_non_thread_channel_resolution_is_unchanged(
    tmp_path: Path, tiers: dict[int, str], expected: str
) -> None:
    """A channel with no ``parent_id`` attribute resolves exactly as before."""
    config = _config(tmp_path, tiers)
    channel = _FakeChannel(id=_PARENT_ID)

    assert _channel_tier(channel=channel, config=config) == expected
