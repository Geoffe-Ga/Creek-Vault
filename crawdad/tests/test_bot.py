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
    message: Any = _FakeMessage(
        author=_FakeAuthor(id=111),
        channel=channel,
        content="hi",
    )
    await client.on_message(message)

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


class _StubRouter:
    """Async-compatible router stand-in for handler tests."""

    def __init__(self, response: Any) -> None:
        self._response = response
        self.calls: list[dict[str, Any]] = []

    async def extract_intents(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if isinstance(self._response, Exception):
            raise self._response
        return self._response


class _StubComposer:
    """Async-compatible composer stand-in returning a fixed reply."""

    def __init__(self, reply: str | Exception) -> None:
        self._reply = reply
        self.calls: list[dict[str, Any]] = []

    async def compose(self, **kwargs: Any) -> str:
        self.calls.append(kwargs)
        if isinstance(self._reply, Exception):
            raise self._reply
        assert isinstance(self._reply, str)
        return self._reply


class _StubSession:
    """Stand-in for :class:`crawdad.mcp_client.MCPSession`."""

    def __init__(
        self,
        *,
        replies: dict[str, str] | None = None,
        errors: dict[str, Exception] | None = None,
    ) -> None:
        self.replies = replies or {}
        self.errors = errors or {}

    async def call_tool(
        self, name: str, arguments: dict[str, Any] | None = None
    ) -> str:
        if name in self.errors:
            raise self.errors[name]
        return self.replies.get(name, "")


class _StubMCPClient:
    """Stand-in for :class:`crawdad.mcp_client.MCPClient`."""

    def __init__(self, session: _StubSession) -> None:
        self._session = session

    def connect(self) -> Any:
        session = self._session
        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def _ctx() -> Any:
            yield session

        return _ctx()


async def test_handle_message_runs_full_loop(
    config: CrawDadConfig, session_state: SessionState
) -> None:
    """An allowlisted message flows through router → dispatch → composer."""
    from crawdad.history import ConversationHistory
    from crawdad.intents import Intent, RouterResponse

    # Router emits one tool call, then signals compose.
    router_responses = iter(
        [
            RouterResponse(intents=[Intent(type="creek.state.read")]),
            RouterResponse(intents=[], compose=True),
        ]
    )

    class _SeqRouter:
        async def extract_intents(self, **_kwargs: Any) -> Any:
            return next(router_responses)

    composer = _StubComposer("voice-faithful reply")
    mcp_client = _StubMCPClient(
        _StubSession(replies={"creek.state.read": "state body"})
    )
    history = ConversationHistory()
    channel = _FakeChannel(id=999, sent=[])
    message: Any = _FakeMessage(
        author=_FakeAuthor(id=111),
        channel=channel,
        content="what's surfacing?",
    )

    await handle_message(
        message,
        config=config,
        session_state=session_state,
        bot_user_id=42,
        router=_SeqRouter(),  # type: ignore[arg-type]
        composer=composer,  # type: ignore[arg-type]
        mcp_client=mcp_client,  # type: ignore[arg-type]
        known_tools=("creek.state.read",),
        history=history,
    )

    assert len(channel.sent) == 1
    assert channel.sent[0] == "voice-faithful reply"
    # History records the user turn and the assistant reply.
    entries = history.as_list()
    assert [e.role for e in entries] == ["user", "assistant"]


async def test_handle_message_router_parse_error_uses_soft_reply(
    config: CrawDadConfig, session_state: SessionState
) -> None:
    """Router parse error → loop returns the "rephrase" outcome."""
    from crawdad.router import RouterParseError

    router = _StubRouter(RouterParseError("not JSON"))
    composer = _StubComposer("(unused)")
    channel = _FakeChannel(id=999, sent=[])
    message: Any = _FakeMessage(
        author=_FakeAuthor(id=111),
        channel=channel,
        content="hi",
    )

    await handle_message(
        message,
        config=config,
        session_state=session_state,
        bot_user_id=42,
        router=router,  # type: ignore[arg-type]
        composer=composer,  # type: ignore[arg-type]
        mcp_client=_StubMCPClient(_StubSession()),  # type: ignore[arg-type]
        known_tools=("creek.state.read",),
    )

    assert len(channel.sent) == 1
    assert "rephrase" in channel.sent[0].lower()
    assert composer.calls == []


async def test_handle_message_unknown_intent_uses_soft_reply(
    config: CrawDadConfig, session_state: SessionState
) -> None:
    """Unknown intent → loop returns the "no such tool" outcome."""
    from crawdad.intents import Intent, RouterResponse

    router = _StubRouter(RouterResponse(intents=[Intent(type="creek.bogus")]))
    composer = _StubComposer("(unused)")
    channel = _FakeChannel(id=999, sent=[])
    message: Any = _FakeMessage(
        author=_FakeAuthor(id=111),
        channel=channel,
        content="hi",
    )

    await handle_message(
        message,
        config=config,
        session_state=session_state,
        bot_user_id=42,
        router=router,  # type: ignore[arg-type]
        composer=composer,  # type: ignore[arg-type]
        mcp_client=_StubMCPClient(_StubSession()),  # type: ignore[arg-type]
        known_tools=("creek.state.read",),
    )

    assert len(channel.sent) == 1
    assert "tool" in channel.sent[0].lower()


async def test_handle_message_mcp_unavailable_uses_soft_reply(
    config: CrawDadConfig, session_state: SessionState
) -> None:
    """MCP subprocess death → loop returns the "unreachable" outcome."""
    from crawdad.intents import Intent, RouterResponse
    from crawdad.mcp_client import MCPUnavailableError

    router = _StubRouter(RouterResponse(intents=[Intent(type="creek.state.read")]))
    composer = _StubComposer("(unused)")
    session = _StubSession(
        errors={"creek.state.read": MCPUnavailableError("subprocess died")}
    )
    channel = _FakeChannel(id=999, sent=[])
    message: Any = _FakeMessage(
        author=_FakeAuthor(id=111),
        channel=channel,
        content="hi",
    )

    await handle_message(
        message,
        config=config,
        session_state=session_state,
        bot_user_id=42,
        router=router,  # type: ignore[arg-type]
        composer=composer,  # type: ignore[arg-type]
        mcp_client=_StubMCPClient(session),  # type: ignore[arg-type]
        known_tools=("creek.state.read",),
    )

    assert len(channel.sent) == 1
    assert "unreachable" in channel.sent[0].lower()


async def test_handle_message_truncates_long_composer_output(
    config: CrawDadConfig, session_state: SessionState
) -> None:
    """Composer outputs over the Discord cap are truncated with an ellipsis."""
    from crawdad.intents import RouterResponse

    router = _StubRouter(RouterResponse(intents=[], compose=True))
    composer = _StubComposer("x" * 5000)
    channel = _FakeChannel(id=999, sent=[])
    message: Any = _FakeMessage(
        author=_FakeAuthor(id=111),
        channel=channel,
        content="hi",
    )

    await handle_message(
        message,
        config=config,
        session_state=session_state,
        bot_user_id=42,
        router=router,  # type: ignore[arg-type]
        composer=composer,  # type: ignore[arg-type]
        mcp_client=_StubMCPClient(_StubSession()),  # type: ignore[arg-type]
        known_tools=(),
    )

    assert len(channel.sent) == 1
    assert channel.sent[0].endswith("...")
    assert len(channel.sent[0]) < 5000


async def test_handle_message_without_loop_components_uses_stub_reply(
    config: CrawDadConfig, session_state: SessionState
) -> None:
    """Missing router/composer/mcp_client → FEAT-013 stub reply (test-only path)."""
    channel = _FakeChannel(id=999, sent=[])
    message: Any = _FakeMessage(
        author=_FakeAuthor(id=111),
        channel=channel,
        content="hi",
    )

    await handle_message(
        message,
        config=config,
        session_state=session_state,
        bot_user_id=42,
        router=None,
        composer=None,
        mcp_client=None,
    )

    assert len(channel.sent) == 1
    assert "scaffold" in channel.sent[0].lower()


def test_crawdad_client_registers_slash_commands_when_loop_runner_provided(
    config: CrawDadConfig, session_state: SessionState
) -> None:
    """FEAT-016: providing a ``loop_runner`` registers the six /crawdad commands."""
    from crawdad.bot import CrawDadClient
    from crawdad.slash_commands import CRAWDAD_COMMANDS

    async def _runner(_message: str) -> str:
        return "noop"

    client = CrawDadClient(
        config=config,
        session_state=session_state,
        loop_runner=_runner,
    )

    # The CommandTree exposes ``get_commands`` returning registered Commands.
    names = {cmd.name for cmd in client.tree.get_commands()}
    assert set(CRAWDAD_COMMANDS).issubset(names)


def test_crawdad_client_without_loop_runner_registers_no_commands(
    config: CrawDadConfig, session_state: SessionState
) -> None:
    """No loop runner → no slash commands (test wiring stays uncoupled)."""
    from crawdad.bot import CrawDadClient

    client = CrawDadClient(config=config, session_state=session_state)

    assert list(client.tree.get_commands()) == []


async def test_setup_hook_skips_sync_when_no_loop_runner(
    config: CrawDadConfig,
    session_state: SessionState,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``setup_hook`` is a no-op when slash commands weren't registered."""
    from crawdad.bot import CrawDadClient

    client = CrawDadClient(config=config, session_state=session_state)
    sync_calls = 0

    async def _spy() -> list[Any]:
        nonlocal sync_calls
        sync_calls += 1
        return []

    monkeypatch.setattr(client.tree, "sync", _spy)

    await client.setup_hook()

    assert sync_calls == 0


async def test_setup_hook_syncs_when_loop_runner_provided(
    config: CrawDadConfig,
    session_state: SessionState,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``setup_hook`` triggers a CommandTree sync after slash registration."""
    from crawdad.bot import CrawDadClient

    async def _runner(_message: str) -> str:
        return "noop"

    client = CrawDadClient(
        config=config,
        session_state=session_state,
        loop_runner=_runner,
    )
    sync_calls = 0

    async def _spy() -> list[Any]:
        nonlocal sync_calls
        sync_calls += 1
        return []

    monkeypatch.setattr(client.tree, "sync", _spy)

    await client.setup_hook()

    assert sync_calls == 1
