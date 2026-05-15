"""Tests for ``crawdad.loop`` — the 5-round router/dispatch/compose orchestration."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

import pytest

from crawdad.composer import ComposerFailureError
from crawdad.config import MAX_LOOP_ROUNDS
from crawdad.history import ConversationHistory
from crawdad.intents import Intent, RouterResponse
from crawdad.loop import AgentLoop, LoopOutcome, run_one_turn
from crawdad.mcp_client import MCPUnavailableError
from crawdad.router import RouterParseError
from crawdad.skill_loader import VoiceSkillStack
from crawdad.state import SessionState


@pytest.fixture
def state() -> SessionState:
    return SessionState(
        raw_markdown="snapshot",
        wavelength_snapshot="Phase: **rising** (confidence 0.84)",
        eddies=(),
        threads=(),
        suggested_questions=(),
    )


@pytest.fixture
def skills() -> VoiceSkillStack:
    return VoiceSkillStack(skills=())


class _ScriptedRouter:
    """Returns a different ``RouterResponse`` per call, in order."""

    def __init__(self, scripted: list[Any]) -> None:
        self._scripted = list(scripted)
        self.calls: list[dict[str, Any]] = []

    async def extract_intents(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if not self._scripted:
            raise AssertionError("router called more times than scripted")
        item = self._scripted.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class _ScriptedSession:
    """MCP session stand-in. Maps tool name → reply body."""

    def __init__(
        self,
        *,
        replies: dict[str, str] | None = None,
        errors: dict[str, Exception] | None = None,
    ) -> None:
        self.replies = replies or {}
        self.errors = errors or {}
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call_tool(
        self, name: str, arguments: dict[str, Any] | None = None
    ) -> str:
        self.calls.append((name, arguments or {}))
        if name in self.errors:
            raise self.errors[name]
        return self.replies.get(name, "")


class _ScriptedMCPClient:
    """MCP client stand-in that yields a single session over and over."""

    def __init__(self, session: _ScriptedSession) -> None:
        self._session = session

    def connect(self) -> Any:
        session = self._session

        @asynccontextmanager
        async def _ctx() -> Any:
            yield session

        return _ctx()


class _ScriptedComposer:
    """Returns a fixed reply (or raises) regardless of inputs."""

    def __init__(self, reply: str | Exception) -> None:
        self._reply = reply
        self.calls: list[dict[str, Any]] = []

    async def compose(self, **kwargs: Any) -> str:
        self.calls.append(kwargs)
        if isinstance(self._reply, Exception):
            raise self._reply
        assert isinstance(self._reply, str)
        return self._reply


async def test_loop_single_round_composes_immediately(
    state: SessionState, skills: VoiceSkillStack
) -> None:
    """``compose=true`` on the first router pass goes straight to composer."""
    router = _ScriptedRouter([RouterResponse(intents=[], compose=True)])
    composer = _ScriptedComposer("Hello in your voice.")
    mcp = _ScriptedMCPClient(_ScriptedSession())
    history = ConversationHistory()
    loop = AgentLoop(
        router=router,  # type: ignore[arg-type]
        composer=composer,  # type: ignore[arg-type]
        mcp_client=mcp,  # type: ignore[arg-type]
        known_tools=("creek.state.read",),
        history=history,
        session_state=state,
        skills=skills,
    )

    outcome = await loop.run("hi")

    assert outcome.kind == "composed"
    assert outcome.reply == "Hello in your voice."
    assert len(router.calls) == 1
    assert composer.calls
    # Composer received zero tool results.
    assert composer.calls[0]["tool_results"] == []


async def test_loop_three_rounds_then_compose(
    state: SessionState, skills: VoiceSkillStack
) -> None:
    """Two tool-call rounds followed by ``compose=true`` aggregates results."""
    router = _ScriptedRouter(
        [
            RouterResponse(intents=[Intent(type="creek.state.read")]),
            RouterResponse(intents=[Intent(type="creek.lint")]),
            RouterResponse(intents=[], compose=True),
        ]
    )
    session = _ScriptedSession(
        replies={"creek.state.read": "state body", "creek.lint": "lint body"}
    )
    composer = _ScriptedComposer("final voice-faithful reply")
    loop = AgentLoop(
        router=router,  # type: ignore[arg-type]
        composer=composer,  # type: ignore[arg-type]
        mcp_client=_ScriptedMCPClient(session),  # type: ignore[arg-type]
        known_tools=("creek.state.read", "creek.lint"),
        history=ConversationHistory(),
        session_state=state,
        skills=skills,
    )

    outcome = await loop.run("what's new?")

    assert outcome.kind == "composed"
    assert outcome.reply == "final voice-faithful reply"
    assert len(router.calls) == 3
    # Both tool results reach the composer.
    aggregated = composer.calls[0]["tool_results"]
    assert {r.intent_type for r in aggregated} == {"creek.state.read", "creek.lint"}


async def test_loop_six_rounds_refused_with_documented_message(
    state: SessionState, skills: VoiceSkillStack
) -> None:
    """The 6th router pass without compose=true triggers the cap reply.

    FEAT-015 §pre-decided choice §27: hard cap at MAX_LOOP_ROUNDS.
    """
    scripted = [
        RouterResponse(intents=[Intent(type="creek.state.read")])
        for _ in range(MAX_LOOP_ROUNDS + 1)
    ]
    router = _ScriptedRouter(scripted)
    composer = _ScriptedComposer("(unused — cap should fire first)")
    history = ConversationHistory()
    loop = AgentLoop(
        router=router,  # type: ignore[arg-type]
        composer=composer,  # type: ignore[arg-type]
        mcp_client=_ScriptedMCPClient(
            _ScriptedSession(replies={"creek.state.read": "body"})
        ),  # type: ignore[arg-type]
        known_tools=("creek.state.read",),
        history=history,
        session_state=state,
        skills=skills,
    )

    outcome = await loop.run("loop forever")

    assert outcome.kind == "too_deep"
    assert "back up" in outcome.reply.lower() or "reframe" in outcome.reply.lower()
    # Composer was never reached.
    assert composer.calls == []
    # History was reset on cap.
    assert history.as_list() == []


async def test_loop_paradox_routed_to_save_before_compose(
    state: SessionState, skills: VoiceSkillStack
) -> None:
    """Paradox in tool results triggers a ``creek.save`` before composing.

    FEAT-015 §pre-decided choice §31: paradox surfacing routes to
    ``10-Liminal/Paradoxes/`` via ``creek.save`` (a tool call inside
    the loop). The composer never proposes resolution.
    """
    router = _ScriptedRouter(
        [
            RouterResponse(intents=[Intent(type="creek.lint")]),
            RouterResponse(intents=[], compose=True),
        ]
    )
    session = _ScriptedSession(
        replies={
            "creek.lint": "paradox detected in 03-Eddies/eddy-x",
            "creek.save": "saved to 10-Liminal/Paradoxes/eddy-x.md",
        }
    )
    composer = _ScriptedComposer("named the paradox; did not propose resolution")
    loop = AgentLoop(
        router=router,  # type: ignore[arg-type]
        composer=composer,  # type: ignore[arg-type]
        mcp_client=_ScriptedMCPClient(session),  # type: ignore[arg-type]
        known_tools=("creek.lint", "creek.save"),
        history=ConversationHistory(),
        session_state=state,
        skills=skills,
    )

    outcome = await loop.run("anything surfacing?")

    assert outcome.kind == "composed"
    # The loop injected a `creek.save` call before composing.
    save_calls = [name for name, _ in session.calls if name == "creek.save"]
    assert len(save_calls) == 1
    # The composer's tool_results include the save outcome.
    types = [r.intent_type for r in composer.calls[0]["tool_results"]]
    assert "creek.save" in types


async def test_loop_paradox_save_skipped_when_save_not_advertised(
    state: SessionState, skills: VoiceSkillStack
) -> None:
    """If ``creek.save`` isn't advertised the loop skips the paradox routing.

    The loop never fabricates a tool surface; if the MCP server doesn't
    expose ``creek.save`` (FEAT-011 not yet merged, etc.), composing
    still proceeds — the composer's paradox-tolerance rule still
    applies via its prompt.
    """
    router = _ScriptedRouter(
        [
            RouterResponse(intents=[Intent(type="creek.lint")]),
            RouterResponse(intents=[], compose=True),
        ]
    )
    session = _ScriptedSession(replies={"creek.lint": "paradox detected"})
    composer = _ScriptedComposer("ack")
    loop = AgentLoop(
        router=router,  # type: ignore[arg-type]
        composer=composer,  # type: ignore[arg-type]
        mcp_client=_ScriptedMCPClient(session),  # type: ignore[arg-type]
        known_tools=("creek.lint",),  # no creek.save
        history=ConversationHistory(),
        session_state=state,
        skills=skills,
    )

    outcome = await loop.run("hi")

    assert outcome.kind == "composed"
    save_calls = [name for name, _ in session.calls if name == "creek.save"]
    assert save_calls == []


async def test_loop_router_parse_error_short_circuits(
    state: SessionState, skills: VoiceSkillStack
) -> None:
    """A router parse error returns the documented soft-reply outcome."""
    router = _ScriptedRouter([RouterParseError("bad JSON")])
    composer = _ScriptedComposer("(unused)")
    loop = AgentLoop(
        router=router,  # type: ignore[arg-type]
        composer=composer,  # type: ignore[arg-type]
        mcp_client=_ScriptedMCPClient(_ScriptedSession()),  # type: ignore[arg-type]
        known_tools=("creek.state.read",),
        history=ConversationHistory(),
        session_state=state,
        skills=skills,
    )

    outcome = await loop.run("hi")

    assert outcome.kind == "router_parse_error"
    assert composer.calls == []


async def test_loop_dispatch_unknown_intent_short_circuits(
    state: SessionState, skills: VoiceSkillStack
) -> None:
    """An unknown intent type halts the loop with the documented reply."""
    router = _ScriptedRouter([RouterResponse(intents=[Intent(type="creek.unicorn")])])
    composer = _ScriptedComposer("(unused)")
    loop = AgentLoop(
        router=router,  # type: ignore[arg-type]
        composer=composer,  # type: ignore[arg-type]
        mcp_client=_ScriptedMCPClient(_ScriptedSession()),  # type: ignore[arg-type]
        known_tools=("creek.state.read",),
        history=ConversationHistory(),
        session_state=state,
        skills=skills,
    )

    outcome = await loop.run("hi")

    assert outcome.kind == "unknown_intent"
    assert composer.calls == []


async def test_loop_mcp_unavailable_short_circuits(
    state: SessionState, skills: VoiceSkillStack
) -> None:
    """MCP subprocess death halts the loop with the documented reply."""
    router = _ScriptedRouter(
        [RouterResponse(intents=[Intent(type="creek.state.read")])]
    )
    session = _ScriptedSession(
        errors={"creek.state.read": MCPUnavailableError("subprocess died")}
    )
    composer = _ScriptedComposer("(unused)")
    loop = AgentLoop(
        router=router,  # type: ignore[arg-type]
        composer=composer,  # type: ignore[arg-type]
        mcp_client=_ScriptedMCPClient(session),  # type: ignore[arg-type]
        known_tools=("creek.state.read",),
        history=ConversationHistory(),
        session_state=state,
        skills=skills,
    )

    outcome = await loop.run("hi")

    assert outcome.kind == "mcp_unavailable"
    assert composer.calls == []


async def test_loop_composer_failure_short_circuits(
    state: SessionState, skills: VoiceSkillStack
) -> None:
    """Composer failure (e.g. Sonnet rate-limit) yields the documented reply."""
    router = _ScriptedRouter([RouterResponse(intents=[], compose=True)])
    composer = _ScriptedComposer(ComposerFailureError("Sonnet rate-limited"))
    loop = AgentLoop(
        router=router,  # type: ignore[arg-type]
        composer=composer,  # type: ignore[arg-type]
        mcp_client=_ScriptedMCPClient(_ScriptedSession()),  # type: ignore[arg-type]
        known_tools=(),
        history=ConversationHistory(),
        session_state=state,
        skills=skills,
    )

    outcome = await loop.run("hi")

    assert outcome.kind == "composer_failure"


async def test_loop_empty_intents_without_compose_terminates_safely(
    state: SessionState, skills: VoiceSkillStack
) -> None:
    """``intents=[], compose=False`` from the router is treated as terminal.

    This prevents an infinite no-op loop if Haiku forgets to set
    ``compose: true`` but also stops emitting intents.
    """
    router = _ScriptedRouter([RouterResponse(intents=[], compose=False)])
    composer = _ScriptedComposer("composed anyway")
    loop = AgentLoop(
        router=router,  # type: ignore[arg-type]
        composer=composer,  # type: ignore[arg-type]
        mcp_client=_ScriptedMCPClient(_ScriptedSession()),  # type: ignore[arg-type]
        known_tools=("creek.state.read",),
        history=ConversationHistory(),
        session_state=state,
        skills=skills,
    )

    outcome = await loop.run("hi")

    assert outcome.kind == "composed"
    assert outcome.reply == "composed anyway"


async def test_run_one_turn_returns_loop_outcome(
    state: SessionState, skills: VoiceSkillStack
) -> None:
    """``run_one_turn`` is the entry point :class:`bot` uses; it just delegates."""
    router = _ScriptedRouter([RouterResponse(intents=[], compose=True)])
    composer = _ScriptedComposer("ack")

    outcome = await run_one_turn(
        message="hi",
        router=router,  # type: ignore[arg-type]
        composer=composer,  # type: ignore[arg-type]
        mcp_client=_ScriptedMCPClient(_ScriptedSession()),  # type: ignore[arg-type]
        known_tools=(),
        history=ConversationHistory(),
        session_state=state,
        skills=skills,
    )

    assert isinstance(outcome, LoopOutcome)
    assert outcome.kind == "composed"


async def test_loop_records_user_and_assistant_turns(
    state: SessionState, skills: VoiceSkillStack
) -> None:
    """A composed turn appends both the user message and the assistant reply."""
    router = _ScriptedRouter([RouterResponse(intents=[], compose=True)])
    composer = _ScriptedComposer("voice-faithful reply")
    history = ConversationHistory()
    loop = AgentLoop(
        router=router,  # type: ignore[arg-type]
        composer=composer,  # type: ignore[arg-type]
        mcp_client=_ScriptedMCPClient(_ScriptedSession()),  # type: ignore[arg-type]
        known_tools=(),
        history=history,
        session_state=state,
        skills=skills,
    )

    await loop.run("first turn")

    entries = history.as_list()
    roles = [e.role for e in entries]
    assert roles == ["user", "assistant"]
    assert entries[0].content == "first turn"
    assert entries[1].content == "voice-faithful reply"


async def test_loop_uses_known_intent_handling(
    state: SessionState, skills: VoiceSkillStack
) -> None:
    """Verify the loop forwards privacy_tier_ceiling correctly through dispatch."""
    router = _ScriptedRouter(
        [
            RouterResponse(
                intents=[
                    Intent(
                        type="creek.state.read",
                        privacy_tier_ceiling="personal",
                    )
                ]
            ),
            RouterResponse(intents=[], compose=True),
        ]
    )
    session = _ScriptedSession(replies={"creek.state.read": "body"})
    composer = _ScriptedComposer("done")
    loop = AgentLoop(
        router=router,  # type: ignore[arg-type]
        composer=composer,  # type: ignore[arg-type]
        mcp_client=_ScriptedMCPClient(session),  # type: ignore[arg-type]
        known_tools=("creek.state.read",),
        history=ConversationHistory(),
        session_state=state,
        skills=skills,
    )

    await loop.run("hi")

    name, args = session.calls[0]
    assert name == "creek.state.read"
    assert args["privacy_tier_ceiling"] == "personal"
