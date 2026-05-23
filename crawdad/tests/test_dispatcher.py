"""Tests for ``crawdad.dispatcher``."""

from __future__ import annotations

from typing import Any

import pytest

from crawdad.dispatcher import (
    IntentDispatcher,
    ToolResult,
    UnknownIntentError,
)
from crawdad.intents import ACTIVATE_REGISTER_INTENT_TYPE, Intent, RouterResponse
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


async def test_dispatcher_routes_activate_register_to_switcher() -> None:
    """FEAT-029: ``activate_register`` intents bypass MCP and call the switcher.

    The dispatcher hands the requested register name to the injected
    ``register_switcher`` callable and emits a :class:`ToolResult`
    describing the outcome. No MCP call is made.
    """
    switched: list[str] = []

    def _switcher(name: str) -> bool:
        switched.append(name)
        return True

    session: Any = _FakeSession()
    dispatcher = IntentDispatcher(
        session=session,
        known_tools=("creek.state.read",),
        register_switcher=_switcher,
    )

    results = await dispatcher.dispatch(
        RouterResponse(
            intents=[
                Intent(
                    type=ACTIVATE_REGISTER_INTENT_TYPE,
                    args={"register": "analytic"},
                )
            ]
        )
    )

    assert switched == ["analytic"]
    assert session.calls == []
    assert len(results) == 1
    assert results[0].intent_type == ACTIVATE_REGISTER_INTENT_TYPE
    assert "analytic" in results[0].body


async def test_dispatcher_activate_register_soft_errors_on_unknown_name() -> None:
    """Switcher returning ``False`` produces a soft-error result (no crash)."""

    def _switcher(_name: str) -> bool:
        return False

    session: Any = _FakeSession()
    dispatcher = IntentDispatcher(
        session=session,
        known_tools=(),
        register_switcher=_switcher,
    )

    results = await dispatcher.dispatch(
        RouterResponse(
            intents=[
                Intent(
                    type=ACTIVATE_REGISTER_INTENT_TYPE,
                    args={"register": "nonexistent"},
                )
            ]
        )
    )

    assert len(results) == 1
    body = results[0].body.lower()
    assert "nonexistent" in results[0].body
    assert "unknown" in body or "not" in body or "could not" in body


async def test_dispatcher_activate_register_without_switcher_soft_errors() -> None:
    """If no switcher is wired the dispatcher still produces a soft result.

    Without the soft-fail path an ``activate_register`` intent emitted
    by Haiku in a test or misconfigured runtime would crash the loop.
    """
    session: Any = _FakeSession()
    dispatcher = IntentDispatcher(session=session, known_tools=())

    results = await dispatcher.dispatch(
        RouterResponse(
            intents=[
                Intent(
                    type=ACTIVATE_REGISTER_INTENT_TYPE,
                    args={"register": "praxis"},
                )
            ]
        )
    )

    assert len(results) == 1
    assert "praxis" in results[0].body or "register" in results[0].body.lower()
    assert session.calls == []


async def test_dispatcher_activate_register_missing_register_arg_soft_errors() -> None:
    """A malformed activate_register intent (no ``register`` arg) soft-errors.

    Defensive: Haiku could emit the intent type without filling the
    expected ``args["register"]`` slot. The dispatcher must not raise.
    """

    def _switcher(_name: str) -> bool:
        return True

    session: Any = _FakeSession()
    dispatcher = IntentDispatcher(
        session=session,
        known_tools=(),
        register_switcher=_switcher,
    )

    results = await dispatcher.dispatch(
        RouterResponse(intents=[Intent(type=ACTIVATE_REGISTER_INTENT_TYPE)])
    )

    assert len(results) == 1
    body = results[0].body.lower()
    assert "register" in body


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
