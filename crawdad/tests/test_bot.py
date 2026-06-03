"""Tests for ``crawdad.bot`` — allowlist, replies, subprocess resilience."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from crawdad.bot import handle_message, render_state_unavailable_reply
from crawdad.config import CrawDadConfig
from crawdad.consent import PendingBatchStore
from crawdad.mcp_client import MCPUnavailableError
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
    id: int = 1
    attachments: list[Any] = field(default_factory=list)


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


async def test_handle_message_with_none_state_does_not_dead_end(
    config: CrawDadConfig,
) -> None:
    """#527: ``session_state=None`` no longer hard-blocks free-text.

    Previously the handler dead-ended every free-text turn on the
    "no audit report yet — run ``creek state``" reply whenever
    ``latest.md`` was absent, diverging from the slash-command path
    that never gates on session state. With loop components absent the
    handler now falls through to the FEAT-013 stub reply (and, when the
    loop is wired, runs the loop with ``state=None`` — see
    ``test_handle_message_runs_full_loop_when_session_state_is_none``).
    The ``creek state`` guidance is still surfaced by
    :func:`render_state_unavailable_reply`.
    """
    from crawdad.bot import _STATE_UNAVAILABLE_REPLY

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
    assert channel.sent[0] != _STATE_UNAVAILABLE_REPLY
    assert "scaffold" in channel.sent[0].lower()


async def test_handle_subprocess_unavailable_replies_gracefully(
    config: CrawDadConfig,
) -> None:
    """A simulated MCP subprocess failure produces the documented soft error."""
    from crawdad.bot import render_mcp_unavailable_reply

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


async def test_crawdad_client_on_message_forwards_workflow_runner(
    config: CrawDadConfig,
    session_state: SessionState,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bug #527-B: the client threads ``workflow_runner`` into ``handle_message``.

    Without this wiring a free-text ``crawdad.run_workflow`` intent
    soft-errors even though the runner the slash-command path uses exists.
    """
    from crawdad.bot import CrawDadClient

    seen: dict[str, Any] = {}

    async def _spy(_message: Any, **kwargs: Any) -> None:
        seen["kwargs"] = kwargs

    monkeypatch.setattr("crawdad.bot.handle_message", _spy)

    async def _workflow_runner(_name: str, _inputs: dict[str, str]) -> str:
        return "ran"  # pragma: no cover - identity check only

    client = CrawDadClient(
        config=config,
        session_state=session_state,
        workflow_runner=_workflow_runner,
    )

    class _User:
        id = 7

    object.__setattr__(client, "_connection", None)
    monkeypatch.setattr(type(client), "user", property(lambda _self: _User()))

    channel = _FakeChannel(id=999, sent=[])
    message: Any = _FakeMessage(
        author=_FakeAuthor(id=111),
        channel=channel,
        content="run a workflow",
    )

    await client.on_message(message)

    assert seen["kwargs"]["workflow_runner"] is _workflow_runner


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
    # History records the user turn, the round-1 tool result fed back to
    # the router (#526), and the assistant reply.
    entries = history.as_list()
    assert [e.role for e in entries] == ["user", "tool", "assistant"]
    assert "state body" in entries[1].content


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


async def test_handle_message_respects_configured_max_loop_rounds(
    session_state: SessionState, tmp_path: Path
) -> None:
    """FEAT-036: a configured ``max_loop_rounds`` reaches the agent loop.

    The router scripts three back-to-back tool-call rounds. With the
    config-level cap pinned at 2, the loop must hit ``too_deep`` on the
    third router pass rather than composing.
    """
    from crawdad.intents import Intent, RouterResponse

    config = CrawDadConfig(
        discord_bot_token="t",
        anthropic_api_key="k",
        vault_path=tmp_path,
        allowed_user_ids=[111],
        allowed_channel_ids=[999],
        max_loop_rounds=2,
    )

    router_responses = iter(
        [
            RouterResponse(intents=[Intent(type="creek.state.read")]),
            RouterResponse(intents=[Intent(type="creek.state.read")]),
            RouterResponse(intents=[], compose=True),
        ]
    )

    class _SeqRouter:
        async def extract_intents(self, **_kwargs: Any) -> Any:
            return next(router_responses)

    composer = _StubComposer("(unused - cap should fire first)")
    mcp_client = _StubMCPClient(_StubSession(replies={"creek.state.read": "body"}))
    channel = _FakeChannel(id=999, sent=[])
    message: Any = _FakeMessage(
        author=_FakeAuthor(id=111),
        channel=channel,
        content="loop forever",
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
    )

    assert len(channel.sent) == 1
    assert "back up" in channel.sent[0].lower() or "reframe" in channel.sent[0].lower()
    assert composer.calls == []


async def test_handle_message_runs_full_loop_when_session_state_is_none(
    config: CrawDadConfig,
) -> None:
    """Bug #527-A: free-text with ``session_state=None`` reaches the loop.

    Previously the handler hard-stopped on ``_STATE_UNAVAILABLE_REPLY``
    whenever ``latest.md`` was absent, dead-ending every free-text turn
    while slash commands (which never gate on state) ran fine. The loop
    and composer tolerate ``state=None`` everywhere, so a free-text turn
    must compose a reply instead of bailing.
    """
    from crawdad.bot import _STATE_UNAVAILABLE_REPLY
    from crawdad.history import ConversationHistory
    from crawdad.intents import RouterResponse

    router = _StubRouter(RouterResponse(intents=[], compose=True))
    composer = _StubComposer("voice-faithful reply without state")
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
        session_state=None,
        bot_user_id=42,
        router=router,  # type: ignore[arg-type]
        composer=composer,  # type: ignore[arg-type]
        mcp_client=_StubMCPClient(_StubSession()),  # type: ignore[arg-type]
        known_tools=("creek.state.read",),
        history=history,
    )

    assert len(channel.sent) == 1
    assert channel.sent[0] == "voice-faithful reply without state"
    assert channel.sent[0] != _STATE_UNAVAILABLE_REPLY
    # The composer was invoked with ``state=None`` — the loop ran.
    assert composer.calls and composer.calls[0]["state"] is None


async def test_handle_message_forwards_workflow_runner_to_loop(
    config: CrawDadConfig, session_state: SessionState
) -> None:
    """Bug #527-B: a free-text ``crawdad.run_workflow`` intent invokes the runner.

    The handler must thread the ``workflow_runner`` closure through to
    :func:`run_one_turn` exactly as the slash command path does, so a
    natural-language request to run a workflow actually dispatches the
    runner instead of soft-erroring with "workflow running is
    unavailable".
    """
    from crawdad.history import ConversationHistory
    from crawdad.intents import (
        RUN_WORKFLOW_INTENT_TYPE,
        Intent,
        RouterResponse,
    )

    runner_calls: list[tuple[str, dict[str, str]]] = []

    async def _workflow_runner(name: str, inputs: dict[str, str]) -> str:
        runner_calls.append((name, inputs))
        return "workflow walked: morning-pages"

    # Router emits a run_workflow intent, then signals compose.
    router_responses = iter(
        [
            RouterResponse(
                intents=[
                    Intent(
                        type=RUN_WORKFLOW_INTENT_TYPE,
                        args={"name": "morning-pages", "inputs": {"mood": "low"}},
                    )
                ]
            ),
            RouterResponse(intents=[], compose=True),
        ]
    )

    class _SeqRouter:
        async def extract_intents(self, **_kwargs: Any) -> Any:
            return next(router_responses)

    composer = _StubComposer("composed after workflow")
    history = ConversationHistory()
    channel = _FakeChannel(id=999, sent=[])
    message: Any = _FakeMessage(
        author=_FakeAuthor(id=111),
        channel=channel,
        content="run the morning pages workflow",
    )

    await handle_message(
        message,
        config=config,
        session_state=session_state,
        bot_user_id=42,
        router=_SeqRouter(),  # type: ignore[arg-type]
        composer=composer,  # type: ignore[arg-type]
        mcp_client=_StubMCPClient(_StubSession()),  # type: ignore[arg-type]
        known_tools=(RUN_WORKFLOW_INTENT_TYPE,),
        history=history,
        workflow_runner=_workflow_runner,
    )

    assert runner_calls == [("morning-pages", {"mood": "low"})]
    assert len(channel.sent) == 1
    assert channel.sent[0] == "composed after workflow"


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
    """FEAT-016: providing a ``loop_runner`` registers the /crawdad commands."""
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


# ---------------------------------------------------------------------------
# FEAT-027 — Discord attachment handling
# ---------------------------------------------------------------------------


@dataclass
class _FakeAttachment:
    """Stand-in for ``discord.Attachment`` used by handler tests."""

    filename: str
    size: int
    payload: bytes = b""
    url: str = "https://cdn.example/file"

    async def read(self) -> bytes:
        return self.payload


async def test_attachment_path_downloads_scans_and_prompts_consent(
    config: CrawDadConfig, session_state: SessionState
) -> None:
    """Attachment messages: download → MCP scan → reply with consent prompt."""
    channel = _FakeChannel(id=999, sent=[])
    attachment = _FakeAttachment(
        filename="journal.md",
        size=4,
        payload=b"safe",
    )
    message: Any = _FakeMessage(
        author=_FakeAuthor(id=111),
        channel=channel,
        content="here's my journal",
        id=42,
        attachments=[attachment],
    )

    scan_calls: list[dict[str, Any]] = []

    class _Session:
        async def call_tool(
            self, name: str, arguments: dict[str, Any] | None = None
        ) -> str:
            scan_calls.append({"name": name, "args": arguments})
            return "Scan summary: no findings"

    client = _StubMCPClient(_Session())  # type: ignore[arg-type]

    await handle_message(
        message,
        config=config,
        session_state=session_state,
        bot_user_id=42,
        mcp_client=client,  # type: ignore[arg-type]
        known_tools=("creek.redact.scan",),
    )

    # File landed at deterministic path.
    staged = (
        config.vault_path / "00-Creek-Meta" / "Inbound" / "999" / "42" / "journal.md"
    )
    assert staged.read_bytes() == b"safe"

    # The scan tool was invoked with the staging dir relative to the vault.
    assert len(scan_calls) == 1
    assert scan_calls[0]["name"] == "creek.redact.scan"
    assert scan_calls[0]["args"]["input_path"] == "00-Creek-Meta/Inbound/999/42"

    # Reply mentions staging, scan results, and the consent prompt.
    # Consent goes out as its own message so a long scan body cannot
    # truncate the "did not ingest" trust signal — see
    # ``test_attachment_reply_consent_prompt_survives_long_scan_output``.
    assert len(channel.sent) == 2
    body, consent = channel.sent
    assert "00-Creek-Meta/Inbound/999/42" in body
    assert "Safety scan" in body
    assert "ingest" in consent.lower()


async def test_attachment_path_replies_when_redact_tool_missing(
    config: CrawDadConfig, session_state: SessionState
) -> None:
    """Downloads succeed, but the soft-error fires when the tool isn't advertised."""
    channel = _FakeChannel(id=999, sent=[])
    attachment = _FakeAttachment(filename="note.md", size=4, payload=b"safe")
    message: Any = _FakeMessage(
        author=_FakeAuthor(id=111),
        channel=channel,
        content="",
        id=99,
        attachments=[attachment],
    )

    await handle_message(
        message,
        config=config,
        session_state=session_state,
        bot_user_id=42,
        mcp_client=_StubMCPClient(_StubSession()),  # type: ignore[arg-type]
        known_tools=("creek.state.read",),  # no creek.redact.scan here
    )

    # Two messages: body (with missing-tool soft error) + consent prompt.
    assert len(channel.sent) == 2
    body, consent = channel.sent
    assert "creek.redact.scan" in body
    assert "Run `creek redact --scan" in body
    assert "ingest" in consent.lower()


async def test_attachment_path_uses_soft_reply_when_mcp_dies_during_scan(
    config: CrawDadConfig, session_state: SessionState
) -> None:
    """If creek.redact.scan dies mid-call, the user sees the soft MCP error."""

    channel = _FakeChannel(id=999, sent=[])
    attachment = _FakeAttachment(filename="note.md", size=4, payload=b"safe")
    message: Any = _FakeMessage(
        author=_FakeAuthor(id=111),
        channel=channel,
        content="",
        id=7,
        attachments=[attachment],
    )

    session = _StubSession(
        errors={"creek.redact.scan": MCPUnavailableError("subprocess died")}
    )
    await handle_message(
        message,
        config=config,
        session_state=session_state,
        bot_user_id=42,
        mcp_client=_StubMCPClient(session),  # type: ignore[arg-type]
        known_tools=("creek.redact.scan",),
    )
    # Two messages: body (with unreachable soft error) + consent prompt.
    assert len(channel.sent) == 2
    body, consent = channel.sent
    assert "unreachable" in body.lower()
    assert "ingest" in consent.lower()


async def test_attachment_path_short_circuits_when_all_already_present(
    config: CrawDadConfig, session_state: SessionState
) -> None:
    """A re-upload of identical bytes replies 'already staged' without scanning."""
    channel = _FakeChannel(id=999, sent=[])
    attachment = _FakeAttachment(filename="note.md", size=4, payload=b"safe")

    def _build_message() -> Any:
        return _FakeMessage(
            author=_FakeAuthor(id=111),
            channel=channel,
            content="",
            id=100,
            attachments=[attachment],
        )

    scan_call_count = 0

    class _Session:
        async def call_tool(
            self, _name: str, _args: dict[str, Any] | None = None
        ) -> str:
            nonlocal scan_call_count
            scan_call_count += 1
            return "Scan summary: no findings"

    client = _StubMCPClient(_Session())  # type: ignore[arg-type]

    # First upload — bytes are new; scan runs.
    await handle_message(
        _build_message(),
        config=config,
        session_state=session_state,
        bot_user_id=42,
        mcp_client=client,  # type: ignore[arg-type]
        known_tools=("creek.redact.scan",),
    )
    assert scan_call_count == 1

    # Second upload of identical bytes — already staged; no scan call.
    channel.sent.clear()
    await handle_message(
        _build_message(),
        config=config,
        session_state=session_state,
        bot_user_id=42,
        mcp_client=client,  # type: ignore[arg-type]
        known_tools=("creek.redact.scan",),
    )
    assert scan_call_count == 1
    assert "already staged" in channel.sent[0].lower()


async def test_attachment_path_replies_with_rejection_summary_when_all_rejected(
    config: CrawDadConfig, session_state: SessionState
) -> None:
    """All-rejected uploads reply with the rejection summary and never call MCP."""
    channel = _FakeChannel(id=999, sent=[])
    attachment = _FakeAttachment(
        filename="badness.exe",
        size=10,
        payload=b"MZ\x00\x00",
    )
    message: Any = _FakeMessage(
        author=_FakeAuthor(id=111),
        channel=channel,
        content="",
        id=55,
        attachments=[attachment],
    )

    class _BoomSession:
        async def call_tool(
            self, _name: str, _args: dict[str, Any] | None = None
        ) -> str:
            raise AssertionError("MCP should not be invoked when nothing was accepted")

    await handle_message(
        message,
        config=config,
        session_state=session_state,
        bot_user_id=42,
        mcp_client=_StubMCPClient(_BoomSession()),  # type: ignore[arg-type]
        known_tools=("creek.redact.scan",),
    )
    assert len(channel.sent) == 1
    assert "extension not allowed" in channel.sent[0]
    # No consent prompt when nothing landed.
    assert "ingest" not in channel.sent[0].lower()


async def test_attachment_path_oversized_file_is_rejected_in_reply(
    tmp_path: Path, session_state: SessionState
) -> None:
    """An oversized attachment is rejected without consuming the body."""
    from crawdad.config import AttachmentConfig

    config = CrawDadConfig(
        discord_bot_token="t",
        anthropic_api_key="k",
        vault_path=tmp_path,
        allowed_user_ids=[111],
        allowed_channel_ids=[999],
        attachments=AttachmentConfig(max_size_bytes=8),
    )
    channel = _FakeChannel(id=999, sent=[])
    attachment = _FakeAttachment(filename="big.md", size=1024, payload=b"x" * 1024)
    message: Any = _FakeMessage(
        author=_FakeAuthor(id=111),
        channel=channel,
        content="",
        id=15,
        attachments=[attachment],
    )

    await handle_message(
        message,
        config=config,
        session_state=session_state,
        bot_user_id=42,
        mcp_client=_StubMCPClient(_StubSession()),  # type: ignore[arg-type]
        known_tools=("creek.redact.scan",),
    )
    assert len(channel.sent) == 1
    assert "exceeds max" in channel.sent[0]


async def test_attachment_path_uses_channel_tier_override_when_configured(
    tmp_path: Path, session_state: SessionState
) -> None:
    """FEAT-027: per-channel privacy tier override is forwarded to creek.redact.scan."""
    from crawdad.config import AttachmentConfig

    config = CrawDadConfig(
        discord_bot_token="t",
        anthropic_api_key="k",
        vault_path=tmp_path,
        allowed_user_ids=[111],
        allowed_channel_ids=[999],
        attachments=AttachmentConfig(channel_privacy_tiers={999: "intimate"}),
    )
    channel = _FakeChannel(id=999, sent=[])
    attachment = _FakeAttachment(filename="x.md", size=4, payload=b"text")
    message: Any = _FakeMessage(
        author=_FakeAuthor(id=111),
        channel=channel,
        content="",
        id=11,
        attachments=[attachment],
    )

    captured: list[dict[str, Any]] = []

    class _Session:
        async def call_tool(
            self, _name: str, args: dict[str, Any] | None = None
        ) -> str:
            captured.append(args or {})
            return "Scan complete"

    await handle_message(
        message,
        config=config,
        session_state=session_state,
        bot_user_id=42,
        mcp_client=_StubMCPClient(_Session()),  # type: ignore[arg-type]
        known_tools=("creek.redact.scan",),
    )
    assert captured[0]["privacy_tier_ceiling"] == "intimate"


async def test_attachment_path_short_circuits_before_agent_loop(
    config: CrawDadConfig, session_state: SessionState
) -> None:
    """Attachment messages do NOT run the agent loop — consent is required first."""
    channel = _FakeChannel(id=999, sent=[])
    attachment = _FakeAttachment(filename="x.md", size=4, payload=b"text")
    message: Any = _FakeMessage(
        author=_FakeAuthor(id=111),
        channel=channel,
        content="please ingest these",
        id=8,
        attachments=[attachment],
    )

    class _ExplodingRouter:
        async def extract_intents(self, **_kwargs: Any) -> Any:
            raise AssertionError("router must not run on the same turn as attachments")

    composer = _StubComposer("(unused)")
    await handle_message(
        message,
        config=config,
        session_state=session_state,
        bot_user_id=42,
        router=_ExplodingRouter(),  # type: ignore[arg-type]
        composer=composer,  # type: ignore[arg-type]
        mcp_client=_StubMCPClient(_StubSession()),  # type: ignore[arg-type]
        known_tools=("creek.redact.scan",),
    )
    assert composer.calls == []


async def test_attachment_path_runs_when_session_state_is_none(
    config: CrawDadConfig,
) -> None:
    """FEAT-027 regression: a degraded session must NOT block file staging.

    Reviewer-flagged blocker: previously the ``session_state is None``
    gate fired before the attachment branch, so a user dropping a file
    during a degraded state received "session unavailable" instead of
    a staged file + safety report.
    """
    channel = _FakeChannel(id=999, sent=[])
    attachment = _FakeAttachment(filename="note.md", size=4, payload=b"safe")
    message: Any = _FakeMessage(
        author=_FakeAuthor(id=111),
        channel=channel,
        content="",
        id=314,
        attachments=[attachment],
    )

    class _Session:
        async def call_tool(
            self, _name: str, _args: dict[str, Any] | None = None
        ) -> str:
            return "Scan summary: no findings"

    await handle_message(
        message,
        config=config,
        session_state=None,
        bot_user_id=42,
        mcp_client=_StubMCPClient(_Session()),  # type: ignore[arg-type]
        known_tools=("creek.redact.scan",),
    )

    # File was staged.
    staged = config.vault_path / "00-Creek-Meta" / "Inbound" / "999" / "314" / "note.md"
    assert staged.read_bytes() == b"safe"
    # Two messages: body (safety scan summary) + consent prompt.
    # No "session unavailable" guidance anywhere in the reply.
    assert len(channel.sent) == 2
    joined = "\n".join(channel.sent)
    assert "creek state" not in joined
    assert "Safety scan" in joined
    assert "ingest" in joined.lower()


async def test_run_safety_scan_swallows_unexpected_exceptions(
    config: CrawDadConfig, session_state: SessionState
) -> None:
    """Reviewer-flagged blocker: non-``MCPUnavailableError`` exceptions
    must not crash the handler. The bot still replies with the soft
    error so the user is not left in silence.
    """
    channel = _FakeChannel(id=999, sent=[])
    attachment = _FakeAttachment(filename="note.md", size=4, payload=b"safe")
    message: Any = _FakeMessage(
        author=_FakeAuthor(id=111),
        channel=channel,
        content="",
        id=271,
        attachments=[attachment],
    )

    class _BrokenSession:
        async def call_tool(
            self, _name: str, _args: dict[str, Any] | None = None
        ) -> str:
            raise TimeoutError("upstream timed out")

    await handle_message(
        message,
        config=config,
        session_state=session_state,
        bot_user_id=42,
        mcp_client=_StubMCPClient(_BrokenSession()),  # type: ignore[arg-type]
        known_tools=("creek.redact.scan",),
    )

    # Two messages: body (soft "unreachable" error) + consent prompt.
    assert len(channel.sent) == 2
    body, consent = channel.sent
    assert "unreachable" in body.lower()
    assert "ingest" in consent.lower()


async def test_run_safety_scan_extracts_report_markdown_from_dict_response(
    config: CrawDadConfig, session_state: SessionState
) -> None:
    """Reviewer-flagged: when MCP returns a JSON dict, extract ``report_markdown``.

    A future revision of ``creek.redact.scan`` may return its full
    structured response (statistics + findings + report) instead of
    just the markdown summary. The handler must show the markdown,
    not a JSON blob, to the user.
    """
    import json as _json

    structured = {
        "status": "empty",
        "tool": "creek.redact.scan",
        "report_markdown": "# Redaction Scan Summary\n\nNo findings.\n",
        "statistics": {"files_scanned": 1, "total_findings": 0},
        "findings": [],
    }
    channel = _FakeChannel(id=999, sent=[])
    attachment = _FakeAttachment(filename="note.md", size=4, payload=b"safe")
    message: Any = _FakeMessage(
        author=_FakeAuthor(id=111),
        channel=channel,
        content="",
        id=161,
        attachments=[attachment],
    )

    class _StructuredSession:
        async def call_tool(
            self, _name: str, _args: dict[str, Any] | None = None
        ) -> str:
            return _json.dumps(structured)

    await handle_message(
        message,
        config=config,
        session_state=session_state,
        bot_user_id=42,
        mcp_client=_StubMCPClient(_StructuredSession()),  # type: ignore[arg-type]
        known_tools=("creek.redact.scan",),
    )

    # Two messages: body (with extracted markdown) + consent prompt.
    assert len(channel.sent) == 2
    body, consent = channel.sent
    assert "Redaction Scan Summary" in body
    # Raw JSON should not leak into the reply when report_markdown is present.
    assert '"findings"' not in body
    assert '"statistics"' not in body
    assert "ingest" in consent.lower()


async def test_run_safety_scan_falls_back_for_unparseable_dict_response(
    config: CrawDadConfig, session_state: SessionState
) -> None:
    """When the response looks like a dict but isn't valid JSON, fall back to raw."""
    channel = _FakeChannel(id=999, sent=[])
    attachment = _FakeAttachment(filename="note.md", size=4, payload=b"safe")
    message: Any = _FakeMessage(
        author=_FakeAuthor(id=111),
        channel=channel,
        content="",
        id=162,
        attachments=[attachment],
    )

    # Brace-bracketed but not valid JSON — handler must not crash, and
    # the raw text falls through to the code-fenced section.
    bad_body = "{not really json}"

    class _BadJsonSession:
        async def call_tool(
            self, _name: str, _args: dict[str, Any] | None = None
        ) -> str:
            return bad_body

    await handle_message(
        message,
        config=config,
        session_state=session_state,
        bot_user_id=42,
        mcp_client=_StubMCPClient(_BadJsonSession()),  # type: ignore[arg-type]
        known_tools=("creek.redact.scan",),
    )

    # Two messages: body (raw bad-json passthrough) + consent prompt.
    assert len(channel.sent) == 2
    body, consent = channel.sent
    assert bad_body in body
    assert "Safety scan" in body
    assert "ingest" in consent.lower()


async def test_channel_tier_default_falls_back_to_personal(
    config: CrawDadConfig, session_state: SessionState
) -> None:
    """No override in ``channel_privacy_tiers`` → ceiling defaults to ``personal``."""
    channel = _FakeChannel(id=999, sent=[])
    attachment = _FakeAttachment(filename="note.md", size=4, payload=b"safe")
    message: Any = _FakeMessage(
        author=_FakeAuthor(id=111),
        channel=channel,
        content="",
        id=10,
        attachments=[attachment],
    )

    captured: list[dict[str, Any]] = []

    class _Session:
        async def call_tool(
            self, _name: str, args: dict[str, Any] | None = None
        ) -> str:
            captured.append(args or {})
            return "Scan summary"

    await handle_message(
        message,
        config=config,
        session_state=session_state,
        bot_user_id=42,
        mcp_client=_StubMCPClient(_Session()),  # type: ignore[arg-type]
        known_tools=("creek.redact.scan",),
    )

    assert captured[0]["privacy_tier_ceiling"] == "personal"


async def test_attachment_reply_consent_prompt_survives_long_scan_output(
    config: CrawDadConfig, session_state: SessionState
) -> None:
    """Reviewer-flagged HIGH: a long scan body must not truncate the consent prompt.

    When ``summary + scan_section`` exceeds the Discord cap, the
    ``_truncate_for_discord`` step would silently drop the
    "I did **not** ingest anything" prompt from the tail. The safety
    invariant (no auto-ingest) still holds because the router never
    fires on attachment turns, but the user trust signal would vanish.

    Fix: send the consent prompt as a separate, guaranteed-untruncated
    follow-up message.
    """
    channel = _FakeChannel(id=999, sent=[])
    attachment = _FakeAttachment(filename="note.md", size=4, payload=b"safe")
    message: Any = _FakeMessage(
        author=_FakeAuthor(id=111),
        channel=channel,
        content="",
        id=4242,
        attachments=[attachment],
    )

    # Force the MCP tool to return a body longer than the Discord cap.
    long_body = "finding-line-" + ("x" * 3000)

    class _BigSession:
        async def call_tool(
            self, _name: str, _args: dict[str, Any] | None = None
        ) -> str:
            return long_body

    await handle_message(
        message,
        config=config,
        session_state=session_state,
        bot_user_id=42,
        mcp_client=_StubMCPClient(_BigSession()),  # type: ignore[arg-type]
        known_tools=("creek.redact.scan",),
    )

    # The combined output exceeds the cap, so at least one message
    # must carry the consent prompt verbatim — never truncated away.
    consent_seen = any("did **not** ingest anything" in sent for sent in channel.sent)
    assert consent_seen, channel.sent
    # The scan content also reaches the user (in some message, possibly
    # truncated). Find a chunk of the body in the joined output.
    joined = "\n".join(channel.sent)
    assert "finding-line-" in joined


# ---------------------------------------------------------------------------
# FEAT-034 — Conversational consent flow
# ---------------------------------------------------------------------------


def _make_store(*, ttl: float = 60.0, clock_value: float = 0.0) -> PendingBatchStore:
    """Construct a fresh ``PendingBatchStore`` with a deterministic clock."""
    return PendingBatchStore(ttl_seconds=ttl, clock=lambda: clock_value)


async def _stage_attachment(
    *,
    config: CrawDadConfig,
    session_state: SessionState,
    channel: _FakeChannel,
    attachment: _FakeAttachment,
    pending_batches: PendingBatchStore,
    message_id: int = 100,
    known_tools: tuple[str, ...] = ("creek.redact.scan",),
    scan_body: str = "Scan summary: no findings",
) -> None:
    """Drive ``handle_message`` once with an attachment to seed a pending batch."""

    class _Session:
        async def call_tool(
            self, _name: str, _args: dict[str, Any] | None = None
        ) -> str:
            return scan_body

    message: Any = _FakeMessage(
        author=_FakeAuthor(id=111),
        channel=channel,
        content="",
        id=message_id,
        attachments=[attachment],
    )
    await handle_message(
        message,
        config=config,
        session_state=session_state,
        bot_user_id=42,
        mcp_client=_StubMCPClient(_Session()),  # type: ignore[arg-type]
        known_tools=known_tools,
        pending_batches=pending_batches,
    )


async def test_consent_token_dispatches_ingest_for_resolved_batch(
    config: CrawDadConfig, session_state: SessionState
) -> None:
    """``ingest`` follow-up calls ``creek.ingest`` for every staged file."""
    channel = _FakeChannel(id=999, sent=[])
    store = _make_store()
    await _stage_attachment(
        config=config,
        session_state=session_state,
        channel=channel,
        attachment=_FakeAttachment(filename="note.md", size=4, payload=b"safe"),
        pending_batches=store,
    )
    channel.sent.clear()

    ingest_calls: list[dict[str, Any]] = []

    class _Session:
        async def call_tool(
            self, name: str, arguments: dict[str, Any] | None = None
        ) -> str:
            ingest_calls.append({"name": name, "args": arguments or {}})
            return "ingested fragment-1"

    followup: Any = _FakeMessage(
        author=_FakeAuthor(id=111),
        channel=channel,
        content="ingest",
    )
    await handle_message(
        followup,
        config=config,
        session_state=session_state,
        bot_user_id=42,
        mcp_client=_StubMCPClient(_Session()),  # type: ignore[arg-type]
        known_tools=("creek.ingest",),
        pending_batches=store,
    )

    assert len(ingest_calls) == 1
    call = ingest_calls[0]
    assert call["name"] == "creek.ingest"
    assert call["args"]["source_type"] == "markdown"
    assert call["args"]["input_path"] == "00-Creek-Meta/Inbound/999/100/note.md"
    assert call["args"]["privacy_tier_ceiling"] == "personal"
    assert len(channel.sent) == 1
    assert "Ingest results" in channel.sent[0]
    assert "ingested fragment-1" in channel.sent[0]
    # Batch transitioned to "ingested" so the store still has it.
    stored = store.get(999)
    assert stored is not None
    assert stored.state == "ingested"


async def test_consent_groups_multiple_files_by_inferred_type(
    config: CrawDadConfig, session_state: SessionState
) -> None:
    """Files sharing a type are dispatched together, sorted alphabetically."""
    channel = _FakeChannel(id=999, sent=[])
    store = _make_store()

    # Stage three files in one attachment turn (two markdown, one image).
    attachments = [
        _FakeAttachment(filename="a.md", size=4, payload=b"aaaa"),
        _FakeAttachment(filename="b.md", size=4, payload=b"bbbb"),
        _FakeAttachment(filename="c.png", size=4, payload=b"cccc"),
    ]
    msg: Any = _FakeMessage(
        author=_FakeAuthor(id=111),
        channel=channel,
        content="",
        id=200,
        attachments=attachments,
    )

    class _ScanSession:
        async def call_tool(
            self, _name: str, _args: dict[str, Any] | None = None
        ) -> str:
            return "no findings"

    await handle_message(
        msg,
        config=config,
        session_state=session_state,
        bot_user_id=42,
        mcp_client=_StubMCPClient(_ScanSession()),  # type: ignore[arg-type]
        known_tools=("creek.redact.scan",),
        pending_batches=store,
    )
    channel.sent.clear()

    ingest_calls: list[dict[str, Any]] = []

    class _IngestSession:
        async def call_tool(
            self, name: str, arguments: dict[str, Any] | None = None
        ) -> str:
            ingest_calls.append({"name": name, "args": arguments or {}})
            return "ok"

    followup: Any = _FakeMessage(
        author=_FakeAuthor(id=111),
        channel=channel,
        content="yes",
    )
    await handle_message(
        followup,
        config=config,
        session_state=session_state,
        bot_user_id=42,
        mcp_client=_StubMCPClient(_IngestSession()),  # type: ignore[arg-type]
        known_tools=("creek.ingest",),
        pending_batches=store,
    )

    # Three calls total: image then both markdowns (alphabetical type order).
    types = [c["args"]["source_type"] for c in ingest_calls]
    assert types == ["image", "markdown", "markdown"]
    # Markdown files preserve upload order.
    md_paths = [
        c["args"]["input_path"]
        for c in ingest_calls
        if c["args"]["source_type"] == "markdown"
    ]
    assert md_paths == [
        "00-Creek-Meta/Inbound/999/200/a.md",
        "00-Creek-Meta/Inbound/999/200/b.md",
    ]


async def test_consent_type_disambiguation_question_then_ingest(
    tmp_path: Path, session_state: SessionState
) -> None:
    """Unresolved-type files trigger a question; the typed reply completes ingest."""
    from crawdad.config import AttachmentConfig

    # Permit the .xyz extension so the unknown-type path can be exercised
    # — the default allow list rejects unknown extensions at the boundary.
    config = CrawDadConfig(
        discord_bot_token="t",
        anthropic_api_key="k",
        vault_path=tmp_path,
        allowed_user_ids=[111],
        allowed_channel_ids=[999],
        attachments=AttachmentConfig(allowed_extensions=frozenset()),
    )
    channel = _FakeChannel(id=999, sent=[])
    store = _make_store()

    # Stage one file with an unknown (unrecognised) extension.
    msg: Any = _FakeMessage(
        author=_FakeAuthor(id=111),
        channel=channel,
        content="",
        id=300,
        attachments=[_FakeAttachment(filename="weird.xyz", size=4, payload=b"data")],
    )

    class _ScanSession:
        async def call_tool(
            self, _name: str, _args: dict[str, Any] | None = None
        ) -> str:
            return "no findings"

    await handle_message(
        msg,
        config=config,
        session_state=session_state,
        bot_user_id=42,
        mcp_client=_StubMCPClient(_ScanSession()),  # type: ignore[arg-type]
        known_tools=("creek.redact.scan",),
        pending_batches=store,
    )
    channel.sent.clear()

    # User says "ingest" — bot must ask for a type, not guess.
    consent_msg: Any = _FakeMessage(
        author=_FakeAuthor(id=111),
        channel=channel,
        content="ingest",
    )
    await handle_message(
        consent_msg,
        config=config,
        session_state=session_state,
        bot_user_id=42,
        mcp_client=_StubMCPClient(_StubSession()),  # type: ignore[arg-type]
        known_tools=("creek.ingest",),
        pending_batches=store,
    )
    assert len(channel.sent) == 1
    assert "`weird.xyz`" in channel.sent[0]
    assert "markdown" in channel.sent[0]  # valid types listed
    stored = store.get(999)
    assert stored is not None
    assert stored.state == "awaiting_type"
    channel.sent.clear()

    # User replies with a valid type — bot completes the ingest.
    ingest_calls: list[dict[str, Any]] = []

    class _IngestSession:
        async def call_tool(
            self, name: str, arguments: dict[str, Any] | None = None
        ) -> str:
            ingest_calls.append({"name": name, "args": arguments or {}})
            return "ok"

    type_msg: Any = _FakeMessage(
        author=_FakeAuthor(id=111),
        channel=channel,
        content="document",
    )
    await handle_message(
        type_msg,
        config=config,
        session_state=session_state,
        bot_user_id=42,
        mcp_client=_StubMCPClient(_IngestSession()),  # type: ignore[arg-type]
        known_tools=("creek.ingest",),
        pending_batches=store,
    )

    assert len(ingest_calls) == 1
    assert ingest_calls[0]["args"]["source_type"] == "document"
    stored = store.get(999)
    assert stored is not None
    assert stored.state == "ingested"


async def test_consent_type_word_falls_through_when_not_awaiting_type(
    config: CrawDadConfig, session_state: SessionState
) -> None:
    """A type word arriving outside ``awaiting_type`` falls through to the loop."""
    from crawdad.intents import RouterResponse

    channel = _FakeChannel(id=999, sent=[])
    store = _make_store()
    await _stage_attachment(
        config=config,
        session_state=session_state,
        channel=channel,
        attachment=_FakeAttachment(filename="note.md", size=4, payload=b"safe"),
        pending_batches=store,
    )
    channel.sent.clear()

    # The batch is in awaiting_consent state. A bare "markdown" should
    # NOT match the consent flow (no consent token yet) — fall through.
    router = _StubRouter(RouterResponse(intents=[], compose=True))
    composer = _StubComposer("agent-loop reply")

    follow: Any = _FakeMessage(
        author=_FakeAuthor(id=111),
        channel=channel,
        content="markdown",
    )
    await handle_message(
        follow,
        config=config,
        session_state=session_state,
        bot_user_id=42,
        router=router,  # type: ignore[arg-type]
        composer=composer,  # type: ignore[arg-type]
        mcp_client=_StubMCPClient(_StubSession()),  # type: ignore[arg-type]
        known_tools=("creek.ingest",),
        pending_batches=store,
    )

    assert channel.sent == ["agent-loop reply"]
    # Batch still in awaiting_consent — type word didn't disturb it.
    stored = store.get(999)
    assert stored is not None
    assert stored.state == "awaiting_consent"


async def test_consent_abandon_clears_pending_batch(
    config: CrawDadConfig, session_state: SessionState
) -> None:
    """``cancel`` / ``drop`` clear the batch without dispatching ingest."""
    channel = _FakeChannel(id=999, sent=[])
    store = _make_store()
    await _stage_attachment(
        config=config,
        session_state=session_state,
        channel=channel,
        attachment=_FakeAttachment(filename="note.md", size=4, payload=b"safe"),
        pending_batches=store,
    )
    channel.sent.clear()

    class _NoIngestSession:
        async def call_tool(
            self, _name: str, _args: dict[str, Any] | None = None
        ) -> str:
            raise AssertionError("creek.ingest must not run after abandonment")

    cancel: Any = _FakeMessage(
        author=_FakeAuthor(id=111),
        channel=channel,
        content="cancel",
    )
    await handle_message(
        cancel,
        config=config,
        session_state=session_state,
        bot_user_id=42,
        mcp_client=_StubMCPClient(_NoIngestSession()),  # type: ignore[arg-type]
        known_tools=("creek.ingest",),
        pending_batches=store,
    )

    assert len(channel.sent) == 1
    assert "cleared" in channel.sent[0].lower()
    assert store.get(999) is None


async def test_consent_idempotent_re_consent_returns_already_ingested(
    config: CrawDadConfig, session_state: SessionState
) -> None:
    """A second ``ingest`` after a successful ingest is a no-op with a clear reply."""
    channel = _FakeChannel(id=999, sent=[])
    store = _make_store()
    await _stage_attachment(
        config=config,
        session_state=session_state,
        channel=channel,
        attachment=_FakeAttachment(filename="note.md", size=4, payload=b"safe"),
        pending_batches=store,
    )
    channel.sent.clear()

    ingest_call_count = 0

    class _Session:
        async def call_tool(
            self, _name: str, _args: dict[str, Any] | None = None
        ) -> str:
            nonlocal ingest_call_count
            ingest_call_count += 1
            return "ok"

    # First consent — should dispatch.
    first: Any = _FakeMessage(
        author=_FakeAuthor(id=111),
        channel=channel,
        content="ingest",
    )
    await handle_message(
        first,
        config=config,
        session_state=session_state,
        bot_user_id=42,
        mcp_client=_StubMCPClient(_Session()),  # type: ignore[arg-type]
        known_tools=("creek.ingest",),
        pending_batches=store,
    )
    assert ingest_call_count == 1
    channel.sent.clear()

    # Second consent — must NOT dispatch again, must reply "already ingested".
    second: Any = _FakeMessage(
        author=_FakeAuthor(id=111),
        channel=channel,
        content="ingest",
    )
    await handle_message(
        second,
        config=config,
        session_state=session_state,
        bot_user_id=42,
        mcp_client=_StubMCPClient(_Session()),  # type: ignore[arg-type]
        known_tools=("creek.ingest",),
        pending_batches=store,
    )

    assert ingest_call_count == 1
    assert len(channel.sent) == 1
    assert "already ingested" in channel.sent[0].lower()


async def test_consent_pending_batch_expires_after_ttl(
    config: CrawDadConfig, session_state: SessionState
) -> None:
    """A stale ``ingest`` after the TTL window falls through to the agent loop."""
    from crawdad.consent import PendingBatchStore
    from crawdad.intents import RouterResponse

    channel = _FakeChannel(id=999, sent=[])
    clock = [0.0]
    store = PendingBatchStore(ttl_seconds=5.0, clock=lambda: clock[0])
    await _stage_attachment(
        config=config,
        session_state=session_state,
        channel=channel,
        attachment=_FakeAttachment(filename="note.md", size=4, payload=b"safe"),
        pending_batches=store,
    )
    channel.sent.clear()

    # Advance clock past TTL — batch expired.
    clock[0] = 100.0

    # Router fires (we hit the agent-loop path), so wire it through.
    router = _StubRouter(RouterResponse(intents=[], compose=True))
    composer = _StubComposer("agent reply")

    follow: Any = _FakeMessage(
        author=_FakeAuthor(id=111),
        channel=channel,
        content="ingest",
    )
    await handle_message(
        follow,
        config=config,
        session_state=session_state,
        bot_user_id=42,
        router=router,  # type: ignore[arg-type]
        composer=composer,  # type: ignore[arg-type]
        mcp_client=_StubMCPClient(_StubSession()),  # type: ignore[arg-type]
        known_tools=("creek.ingest",),
        pending_batches=store,
    )

    assert channel.sent == ["agent reply"]
    assert store.get(999) is None


async def test_consent_unrelated_message_falls_through_to_agent_loop(
    config: CrawDadConfig, session_state: SessionState
) -> None:
    """Non-consent text with a live pending batch still runs the agent loop."""
    from crawdad.intents import RouterResponse

    channel = _FakeChannel(id=999, sent=[])
    store = _make_store()
    await _stage_attachment(
        config=config,
        session_state=session_state,
        channel=channel,
        attachment=_FakeAttachment(filename="note.md", size=4, payload=b"safe"),
        pending_batches=store,
    )
    channel.sent.clear()

    router = _StubRouter(RouterResponse(intents=[], compose=True))
    composer = _StubComposer("voice reply")

    follow: Any = _FakeMessage(
        author=_FakeAuthor(id=111),
        channel=channel,
        content="what is surfacing today?",
    )
    await handle_message(
        follow,
        config=config,
        session_state=session_state,
        bot_user_id=42,
        router=router,  # type: ignore[arg-type]
        composer=composer,  # type: ignore[arg-type]
        mcp_client=_StubMCPClient(_StubSession()),  # type: ignore[arg-type]
        known_tools=("creek.ingest",),
        pending_batches=store,
    )

    assert channel.sent == ["voice reply"]
    # Batch is still there — unrelated chatter must not consume it.
    assert store.get(999) is not None


async def test_consent_soft_error_when_ingest_tool_not_advertised(
    config: CrawDadConfig, session_state: SessionState
) -> None:
    """Consent reply with ``creek.ingest`` not advertised → documented soft error."""
    channel = _FakeChannel(id=999, sent=[])
    store = _make_store()
    await _stage_attachment(
        config=config,
        session_state=session_state,
        channel=channel,
        attachment=_FakeAttachment(filename="note.md", size=4, payload=b"safe"),
        pending_batches=store,
    )
    channel.sent.clear()

    follow: Any = _FakeMessage(
        author=_FakeAuthor(id=111),
        channel=channel,
        content="ingest",
    )
    await handle_message(
        follow,
        config=config,
        session_state=session_state,
        bot_user_id=42,
        mcp_client=_StubMCPClient(_StubSession()),  # type: ignore[arg-type]
        known_tools=("creek.redact.scan",),  # no creek.ingest
        pending_batches=store,
    )

    assert len(channel.sent) == 1
    assert "creek.ingest" in channel.sent[0]


async def test_consent_soft_error_when_mcp_unavailable_during_ingest(
    config: CrawDadConfig, session_state: SessionState
) -> None:
    """MCP subprocess death mid-ingest → documented soft "unreachable" reply."""
    channel = _FakeChannel(id=999, sent=[])
    store = _make_store()
    await _stage_attachment(
        config=config,
        session_state=session_state,
        channel=channel,
        attachment=_FakeAttachment(filename="note.md", size=4, payload=b"safe"),
        pending_batches=store,
    )
    channel.sent.clear()

    session = _StubSession(
        errors={"creek.ingest": MCPUnavailableError("subprocess died")}
    )
    follow: Any = _FakeMessage(
        author=_FakeAuthor(id=111),
        channel=channel,
        content="ingest",
    )
    await handle_message(
        follow,
        config=config,
        session_state=session_state,
        bot_user_id=42,
        mcp_client=_StubMCPClient(session),  # type: ignore[arg-type]
        known_tools=("creek.ingest",),
        pending_batches=store,
    )

    assert len(channel.sent) == 1
    assert "unreachable" in channel.sent[0].lower()
    # The batch did NOT transition to "ingested" — nothing landed.
    stored = store.get(999)
    assert stored is not None
    assert stored.state == "awaiting_consent"


async def test_attachment_turn_supersedes_prior_pending_batch(
    config: CrawDadConfig, session_state: SessionState
) -> None:
    """A fresh attachment turn overwrites any prior pending batch for the channel."""
    channel = _FakeChannel(id=999, sent=[])
    store = _make_store()

    # First batch — note.md.
    await _stage_attachment(
        config=config,
        session_state=session_state,
        channel=channel,
        attachment=_FakeAttachment(filename="note.md", size=4, payload=b"safe"),
        pending_batches=store,
        message_id=100,
    )
    first = store.get(999)
    assert first is not None
    assert first.files[0].filename == "note.md"
    channel.sent.clear()

    # Second batch — image.png. Should overwrite.
    await _stage_attachment(
        config=config,
        session_state=session_state,
        channel=channel,
        attachment=_FakeAttachment(filename="image.png", size=4, payload=b"png!"),
        pending_batches=store,
        message_id=200,
    )

    second = store.get(999)
    assert second is not None
    assert second.files[0].filename == "image.png"
    assert second is not first


async def test_consent_with_pending_batches_none_falls_through(
    config: CrawDadConfig, session_state: SessionState
) -> None:
    """Bot still works when the store isn't wired (test/unwired path)."""
    from crawdad.intents import RouterResponse

    channel = _FakeChannel(id=999, sent=[])
    router = _StubRouter(RouterResponse(intents=[], compose=True))
    composer = _StubComposer("agent reply")

    follow: Any = _FakeMessage(
        author=_FakeAuthor(id=111),
        channel=channel,
        content="ingest",
    )
    await handle_message(
        follow,
        config=config,
        session_state=session_state,
        bot_user_id=42,
        router=router,  # type: ignore[arg-type]
        composer=composer,  # type: ignore[arg-type]
        mcp_client=_StubMCPClient(_StubSession()),  # type: ignore[arg-type]
        known_tools=("creek.ingest",),
        # pending_batches not provided — defaults to None
    )

    assert channel.sent == ["agent reply"]


async def test_consent_forwards_channel_tier_override(
    tmp_path: Path, session_state: SessionState
) -> None:
    """The privacy tier from ``channel_privacy_tiers`` reaches every ingest call."""
    from crawdad.config import AttachmentConfig

    config = CrawDadConfig(
        discord_bot_token="t",
        anthropic_api_key="k",
        vault_path=tmp_path,
        allowed_user_ids=[111],
        allowed_channel_ids=[999],
        attachments=AttachmentConfig(channel_privacy_tiers={999: "intimate"}),
    )
    channel = _FakeChannel(id=999, sent=[])
    store = _make_store()
    await _stage_attachment(
        config=config,
        session_state=session_state,
        channel=channel,
        attachment=_FakeAttachment(filename="note.md", size=4, payload=b"safe"),
        pending_batches=store,
    )
    channel.sent.clear()

    ingest_calls: list[dict[str, Any]] = []

    class _Session:
        async def call_tool(
            self, name: str, arguments: dict[str, Any] | None = None
        ) -> str:
            ingest_calls.append({"name": name, "args": arguments or {}})
            return "ok"

    follow: Any = _FakeMessage(
        author=_FakeAuthor(id=111),
        channel=channel,
        content="ingest",
    )
    await handle_message(
        follow,
        config=config,
        session_state=session_state,
        bot_user_id=42,
        mcp_client=_StubMCPClient(_Session()),  # type: ignore[arg-type]
        known_tools=("creek.ingest",),
        pending_batches=store,
    )

    assert ingest_calls[0]["args"]["privacy_tier_ceiling"] == "intimate"


async def test_consent_retry_after_partial_failure_skips_already_ingested(
    config: CrawDadConfig, session_state: SessionState
) -> None:
    """A mid-batch MCP failure lets the user retry; landed files are skipped."""
    channel = _FakeChannel(id=999, sent=[])
    store = _make_store()

    # Stage two markdown files in one turn.
    attachments = [
        _FakeAttachment(filename="a.md", size=4, payload=b"aaaa"),
        _FakeAttachment(filename="b.md", size=4, payload=b"bbbb"),
    ]
    msg: Any = _FakeMessage(
        author=_FakeAuthor(id=111),
        channel=channel,
        content="",
        id=500,
        attachments=attachments,
    )

    class _ScanSession:
        async def call_tool(
            self, _name: str, _args: dict[str, Any] | None = None
        ) -> str:
            return "no findings"

    await handle_message(
        msg,
        config=config,
        session_state=session_state,
        bot_user_id=42,
        mcp_client=_StubMCPClient(_ScanSession()),  # type: ignore[arg-type]
        known_tools=("creek.redact.scan",),
        pending_batches=store,
    )
    channel.sent.clear()

    # First consent: dispatcher succeeds on file 1, dies on file 2.
    call_index = {"n": 0}

    class _FlakySession:
        async def call_tool(
            self, _name: str, _args: dict[str, Any] | None = None
        ) -> str:
            call_index["n"] += 1
            if call_index["n"] == 1:
                return "ingested fragment-a"
            raise MCPUnavailableError("subprocess died mid-batch")

    first: Any = _FakeMessage(
        author=_FakeAuthor(id=111),
        channel=channel,
        content="ingest",
    )
    await handle_message(
        first,
        config=config,
        session_state=session_state,
        bot_user_id=42,
        mcp_client=_StubMCPClient(_FlakySession()),  # type: ignore[arg-type]
        known_tools=("creek.ingest",),
        pending_batches=store,
    )
    # User saw the soft-error reply.
    assert any("unreachable" in s.lower() for s in channel.sent)
    # Batch stays in awaiting_consent so retries are accepted.
    stored = store.get(999)
    assert stored is not None
    assert stored.state == "awaiting_consent"
    # file-a's hash landed on the ingested set so a retry skips it.
    assert len(stored.ingested_hashes) == 1
    channel.sent.clear()

    # Second consent: dispatcher succeeds on file 2.
    second_calls: list[dict[str, Any]] = []

    class _Session:
        async def call_tool(
            self, name: str, arguments: dict[str, Any] | None = None
        ) -> str:
            second_calls.append({"name": name, "args": arguments or {}})
            return "ingested fragment-b"

    retry: Any = _FakeMessage(
        author=_FakeAuthor(id=111),
        channel=channel,
        content="ingest",
    )
    await handle_message(
        retry,
        config=config,
        session_state=session_state,
        bot_user_id=42,
        mcp_client=_StubMCPClient(_Session()),  # type: ignore[arg-type]
        known_tools=("creek.ingest",),
        pending_batches=store,
    )

    # Only file b is dispatched on the retry — file a is already on the ingested set.
    assert len(second_calls) == 1
    assert second_calls[0]["args"]["input_path"].endswith("b.md")
    # Reply mentions both files: file a as "already ingested", file b with its body.
    reply = channel.sent[-1]
    assert "already ingested" in reply
    assert "ingested fragment-b" in reply
    # Now everything has landed → state transitioned.
    final = store.get(999)
    assert final is not None
    assert final.state == "ingested"


async def test_consent_dispatch_with_staged_path_outside_vault(
    config: CrawDadConfig, session_state: SessionState
) -> None:
    """Files staged outside the vault still ingest with the absolute path.

    Defends against a future code change where the staging dir gets
    relocated outside ``config.vault_path`` — the ingest call should
    keep working with the absolute path instead of crashing.
    """
    from crawdad.consent import PendingFile, build_pending_batch

    channel = _FakeChannel(id=999, sent=[])
    store = _make_store()
    outside_path = Path("/var/tmp/staging/note.md")
    pf = PendingFile(
        filename="note.md",
        original_filename="note.md",
        staged_path=outside_path,
        content_hash="h-outside",
        inferred_type="markdown",
    )
    batch = build_pending_batch(
        channel_id=999,
        staging_dir=outside_path.parent,
        accepted_files=(pf,),
        privacy_tier_ceiling="personal",
        now=store.now(),
    )
    store.record(batch)

    captured: list[dict[str, Any]] = []

    class _Session:
        async def call_tool(
            self, name: str, arguments: dict[str, Any] | None = None
        ) -> str:
            captured.append({"name": name, "args": arguments or {}})
            return "ok"

    follow: Any = _FakeMessage(
        author=_FakeAuthor(id=111),
        channel=channel,
        content="ingest",
    )
    await handle_message(
        follow,
        config=config,
        session_state=session_state,
        bot_user_id=42,
        mcp_client=_StubMCPClient(_Session()),  # type: ignore[arg-type]
        known_tools=("creek.ingest",),
        pending_batches=store,
    )

    # The absolute path is forwarded verbatim because relative_to() raised.
    assert captured[0]["args"]["input_path"] == str(outside_path)


async def test_consent_concurrent_followups_dispatch_only_once(
    config: CrawDadConfig, session_state: SessionState
) -> None:
    """Two concurrent ``ingest`` messages on the same channel must not double-dispatch.

    Reviewer-flagged HIGH: without per-channel locking, both coroutines
    can read ``state == "awaiting_consent"`` before either completes
    its dispatch, double-firing ``creek.ingest`` for the same file
    (PR #308). The per-channel lock from
    :meth:`PendingBatchStore.lock_for` serialises the critical
    section; the second message sees ``state == "ingested"`` and
    replies "already ingested".
    """
    import asyncio

    channel = _FakeChannel(id=999, sent=[])
    store = _make_store()
    await _stage_attachment(
        config=config,
        session_state=session_state,
        channel=channel,
        attachment=_FakeAttachment(filename="note.md", size=4, payload=b"safe"),
        pending_batches=store,
    )
    channel.sent.clear()

    barrier = asyncio.Event()
    call_count = 0

    class _SlowSession:
        async def call_tool(
            self, _name: str, _arguments: dict[str, Any] | None = None
        ) -> str:
            nonlocal call_count
            call_count += 1
            await barrier.wait()
            return "ingested fragment-1"

    mcp = _StubMCPClient(_SlowSession())

    msg_a: Any = _FakeMessage(
        author=_FakeAuthor(id=111),
        channel=channel,
        content="ingest",
        id=2001,
    )
    msg_b: Any = _FakeMessage(
        author=_FakeAuthor(id=111),
        channel=channel,
        content="ingest",
        id=2002,
    )

    task_a = asyncio.create_task(
        handle_message(
            msg_a,
            config=config,
            session_state=session_state,
            bot_user_id=42,
            mcp_client=mcp,  # type: ignore[arg-type]
            known_tools=("creek.ingest",),
            pending_batches=store,
        )
    )
    # Yield once so task A starts and grabs the lock before B is scheduled.
    await asyncio.sleep(0)
    task_b = asyncio.create_task(
        handle_message(
            msg_b,
            config=config,
            session_state=session_state,
            bot_user_id=42,
            mcp_client=mcp,  # type: ignore[arg-type]
            known_tools=("creek.ingest",),
            pending_batches=store,
        )
    )
    # Let task B reach the lock-acquisition point.
    await asyncio.sleep(0)
    # Release task A's dispatch so the critical section can finish.
    barrier.set()

    await asyncio.gather(task_a, task_b)

    # Only one ``creek.ingest`` dispatch happened across both messages.
    assert call_count == 1
    # The second message received the "already ingested" reply.
    assert any("already ingested" in s.lower() for s in channel.sent)
    stored = store.get(999)
    assert stored is not None
    assert stored.state == "ingested"


async def test_run_ingest_dispatch_swallows_unexpected_exceptions(
    config: CrawDadConfig, session_state: SessionState
) -> None:
    """Non-``MCPUnavailableError`` exceptions are caught — user is never left silent.

    Reviewer-flagged LOW: a TimeoutError (or any other surprise) from
    the MCP session would otherwise propagate out of
    ``handle_message`` and the user would see no reply. Mirrors the
    existing ``_run_safety_scan`` behaviour.
    """
    channel = _FakeChannel(id=999, sent=[])
    store = _make_store()
    await _stage_attachment(
        config=config,
        session_state=session_state,
        channel=channel,
        attachment=_FakeAttachment(filename="note.md", size=4, payload=b"safe"),
        pending_batches=store,
    )
    channel.sent.clear()

    class _BrokenSession:
        async def call_tool(
            self, _name: str, _args: dict[str, Any] | None = None
        ) -> str:
            raise TimeoutError("upstream timed out")

    follow: Any = _FakeMessage(
        author=_FakeAuthor(id=111),
        channel=channel,
        content="ingest",
    )
    await handle_message(
        follow,
        config=config,
        session_state=session_state,
        bot_user_id=42,
        mcp_client=_StubMCPClient(_BrokenSession()),  # type: ignore[arg-type]
        known_tools=("creek.ingest",),
        pending_batches=store,
    )

    assert len(channel.sent) == 1
    assert "unreachable" in channel.sent[0].lower()
    # The batch was not marked ingested — user can retry.
    stored = store.get(999)
    assert stored is not None
    assert stored.state == "awaiting_consent"


async def test_already_present_batch_does_not_record_pending_state(
    config: CrawDadConfig, session_state: SessionState
) -> None:
    """A duplicate upload returns ``already staged`` without seeding a batch."""
    channel = _FakeChannel(id=999, sent=[])
    store = _make_store()
    attachment = _FakeAttachment(filename="dup.md", size=4, payload=b"same")

    def _build() -> Any:
        return _FakeMessage(
            author=_FakeAuthor(id=111),
            channel=channel,
            content="",
            id=400,
            attachments=[attachment],
        )

    class _ScanSession:
        async def call_tool(
            self, _name: str, _args: dict[str, Any] | None = None
        ) -> str:
            return "no findings"

    # First upload — scan + record.
    await handle_message(
        _build(),
        config=config,
        session_state=session_state,
        bot_user_id=42,
        mcp_client=_StubMCPClient(_ScanSession()),  # type: ignore[arg-type]
        known_tools=("creek.redact.scan",),
        pending_batches=store,
    )
    # Cancel the first batch so we have a clean slate.
    cancel: Any = _FakeMessage(
        author=_FakeAuthor(id=111),
        channel=channel,
        content="cancel",
    )
    await handle_message(
        cancel,
        config=config,
        session_state=session_state,
        bot_user_id=42,
        mcp_client=_StubMCPClient(_StubSession()),  # type: ignore[arg-type]
        known_tools=("creek.ingest",),
        pending_batches=store,
    )
    assert store.get(999) is None

    # Second upload of identical bytes — already present; no new batch.
    channel.sent.clear()
    await handle_message(
        _build(),
        config=config,
        session_state=session_state,
        bot_user_id=42,
        mcp_client=_StubMCPClient(_ScanSession()),  # type: ignore[arg-type]
        known_tools=("creek.redact.scan",),
        pending_batches=store,
    )

    assert store.get(999) is None
    assert any("already staged" in s.lower() for s in channel.sent)
