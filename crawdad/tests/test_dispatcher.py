"""Tests for ``crawdad.dispatcher``."""

from __future__ import annotations

from typing import Any

import pytest

from crawdad.dispatcher import (
    IntentDispatcher,
    ToolResult,
    UnknownIntentError,
)
from crawdad.intents import Intent, RouterResponse
from crawdad.mcp_client import MCPUnavailableError


class _FakeSession:
    """Stand-in for :class:`crawdad.mcp_client.MCPSession`."""

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


async def test_dispatcher_invokes_each_intent_in_order() -> None:
    """Intents are dispatched 1-to-1 against the MCP session, in order."""
    session: Any = _FakeSession(
        replies={"creek.state.read": "state-body", "creek.mine": "mine-seeds"}
    )
    dispatcher = IntentDispatcher(
        session=session,
        known_tools=("creek.state.read", "creek.mine"),
    )

    response = RouterResponse(
        intents=[
            Intent(type="creek.state.read"),
            Intent(type="creek.mine", args={"phase": "rising"}),
        ]
    )

    results = await dispatcher.dispatch(response)

    assert results == [
        ToolResult(intent_type="creek.state.read", body="state-body"),
        ToolResult(intent_type="creek.mine", body="mine-seeds"),
    ]
    assert session.calls == [
        ("creek.state.read", {"privacy_tier_ceiling": "open"}),
        (
            "creek.mine",
            {"phase": "rising", "privacy_tier_ceiling": "open"},
        ),
    ]


async def test_dispatcher_rejects_unknown_intent_type() -> None:
    """An intent whose ``type`` is not in the advertised tools is refused."""
    session: Any = _FakeSession()
    dispatcher = IntentDispatcher(session=session, known_tools=("creek.state.read",))

    response = RouterResponse(intents=[Intent(type="creek.unicorn")])

    with pytest.raises(UnknownIntentError, match=r"creek\.unicorn"):
        await dispatcher.dispatch(response)

    assert session.calls == []


async def test_dispatcher_returns_empty_list_for_no_intents() -> None:
    """An empty intents array means the router decided no tools were needed."""
    session: Any = _FakeSession()
    dispatcher = IntentDispatcher(session=session, known_tools=("creek.state.read",))

    results = await dispatcher.dispatch(RouterResponse(intents=[]))

    assert results == []
    assert session.calls == []


async def test_dispatcher_propagates_mcp_unavailable() -> None:
    """A subprocess death during dispatch surfaces as MCPUnavailableError."""
    session: Any = _FakeSession(
        errors={"creek.state.read": MCPUnavailableError("subprocess died")}
    )
    dispatcher = IntentDispatcher(session=session, known_tools=("creek.state.read",))

    with pytest.raises(MCPUnavailableError):
        await dispatcher.dispatch(
            RouterResponse(intents=[Intent(type="creek.state.read")])
        )


async def test_dispatcher_preserves_privacy_tier_ceiling() -> None:
    """The router's tier ceiling reaches the MCP call site verbatim."""
    session: Any = _FakeSession(replies={"creek.state.read": "ok"})
    dispatcher = IntentDispatcher(session=session, known_tools=("creek.state.read",))

    await dispatcher.dispatch(
        RouterResponse(
            intents=[Intent(type="creek.state.read", privacy_tier_ceiling="personal")]
        )
    )

    assert session.calls[0][1]["privacy_tier_ceiling"] == "personal"


async def test_dispatcher_intent_ceiling_overrides_conflicting_args() -> None:
    """The Intent's declared ceiling wins over a conflicting key in args.

    Regression for review feedback: Haiku could otherwise smuggle a
    looser tier through ``args["privacy_tier_ceiling"]``. The Intent
    model is authoritative — the MCP server must see the ceiling the
    router declared, not the one Haiku tucked into the args dict.
    """
    session: Any = _FakeSession(replies={"creek.state.read": "ok"})
    dispatcher = IntentDispatcher(session=session, known_tools=("creek.state.read",))

    await dispatcher.dispatch(
        RouterResponse(
            intents=[
                Intent(
                    type="creek.state.read",
                    privacy_tier_ceiling="personal",
                    args={"privacy_tier_ceiling": "all"},  # Haiku tried to smuggle
                )
            ]
        )
    )

    assert session.calls[0][1]["privacy_tier_ceiling"] == "personal"
