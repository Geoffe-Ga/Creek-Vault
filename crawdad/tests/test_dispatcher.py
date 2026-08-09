"""Tests for ``crawdad.dispatcher``."""

from __future__ import annotations

import logging
import re
from typing import Any

import pytest

from crawdad import dispatcher as dispatcher_module
from crawdad.dispatcher import (
    IntentDispatcher,
    ToolResult,
    UnknownIntentError,
)
from crawdad.intents import (
    ACTIVATE_REGISTER_INTENT_TYPE,
    RUN_WORKFLOW_INTENT_TYPE,
    Intent,
    PrivacyTierCeiling,
    RouterResponse,
)
from crawdad.mcp_client import MCPUnavailableError

# The tier ordering these tests hold the dispatcher to, written out here
# rather than imported from ``crawdad.intents.CEILING_RANK``. A test that
# checks an ordering against the very table under test cannot catch a
# permuted table; ``test_intents.py`` pins CEILING_RANK against this same
# order, so the two files corroborate each other.
_RANK_ORDER: tuple[PrivacyTierCeiling, ...] = (
    PrivacyTierCeiling.OPEN,
    PrivacyTierCeiling.PERSONAL,
    PrivacyTierCeiling.INTIMATE,
    PrivacyTierCeiling.ALL,
)
_RANK_ORDER_IDS = [tier.value for tier in _RANK_ORDER]
_ABOVE_CAP = (PrivacyTierCeiling.INTIMATE, PrivacyTierCeiling.ALL)
_ABOVE_CAP_IDS = [tier.value for tier in _ABOVE_CAP]
_ADMITTED = (PrivacyTierCeiling.OPEN, PrivacyTierCeiling.PERSONAL)
_ADMITTED_IDS = [tier.value for tier in _ADMITTED]


def _rank(tier: PrivacyTierCeiling | str) -> int:
    """Return the position of *tier* in the most-restrictive-first order."""
    return _RANK_ORDER.index(PrivacyTierCeiling(tier))


def _report(
    reply: str,
    ceiling: PrivacyTierCeiling = PrivacyTierCeiling.OPEN,
) -> dispatcher_module.WorkflowRunReport:
    """Build the report shape an ADAPT-003 workflow runner returns.

    The runner reports the ceiling the *walk* actually used, which is
    not necessarily the ceiling the router put on the intent.
    """
    return dispatcher_module.WorkflowRunReport(
        reply=reply, privacy_tier_ceiling=ceiling
    )


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


async def test_dispatcher_routes_run_workflow_to_runner() -> None:
    """ADAPT-003: ``run_workflow`` intents bypass MCP and call the runner.

    The dispatcher hands the requested workflow name + inputs to the
    injected ``workflow_runner`` callable and emits a
    :class:`ToolResult` carrying the composed reply. No MCP call is
    made.
    """
    calls: list[tuple[str, dict[str, str]]] = []

    async def _runner(
        name: str, inputs: dict[str, str]
    ) -> dispatcher_module.WorkflowRunReport:
        calls.append((name, inputs))
        return _report(f"ran {name}")

    session: Any = _FakeSession()
    dispatcher = IntentDispatcher(
        session=session,
        known_tools=("creek.state.read",),
        workflow_runner=_runner,
    )

    results = await dispatcher.dispatch(
        RouterResponse(
            intents=[
                Intent(
                    type=RUN_WORKFLOW_INTENT_TYPE,
                    args={
                        "name": "phase-transitions",
                        "inputs": {"topic": "spirals"},
                    },
                )
            ]
        )
    )

    assert calls == [("phase-transitions", {"topic": "spirals"})]
    assert session.calls == []
    assert len(results) == 1
    assert results[0].intent_type == RUN_WORKFLOW_INTENT_TYPE
    assert results[0].body == "ran phase-transitions"


async def test_dispatcher_run_workflow_defaults_inputs_to_empty() -> None:
    """A run_workflow intent without ``inputs`` runs with an empty mapping."""
    captured: dict[str, dict[str, str]] = {}

    async def _runner(
        name: str, inputs: dict[str, str]
    ) -> dispatcher_module.WorkflowRunReport:
        captured[name] = inputs
        return _report("ok")

    session: Any = _FakeSession()
    dispatcher = IntentDispatcher(
        session=session,
        known_tools=(),
        workflow_runner=_runner,
    )

    await dispatcher.dispatch(
        RouterResponse(
            intents=[Intent(type=RUN_WORKFLOW_INTENT_TYPE, args={"name": "weekly"})]
        )
    )

    assert captured == {"weekly": {}}


async def test_dispatcher_run_workflow_coerces_non_string_inputs() -> None:
    """Non-string ``inputs`` values are stringified for the walker."""
    captured: dict[str, str] = {}

    async def _runner(
        _name: str, inputs: dict[str, str]
    ) -> dispatcher_module.WorkflowRunReport:
        captured.update(inputs)
        return _report("ok")

    session: Any = _FakeSession()
    dispatcher = IntentDispatcher(
        session=session,
        known_tools=(),
        workflow_runner=_runner,
    )

    await dispatcher.dispatch(
        RouterResponse(
            intents=[
                Intent(
                    type=RUN_WORKFLOW_INTENT_TYPE,
                    args={"name": "weekly", "inputs": {"count": 3}},
                )
            ]
        )
    )

    assert captured == {"count": "3"}


async def test_dispatcher_run_workflow_ignores_non_mapping_inputs() -> None:
    """A malformed ``inputs`` arg (not an object) degrades to an empty dict."""
    captured: dict[str, dict[str, str]] = {}

    async def _runner(
        name: str, inputs: dict[str, str]
    ) -> dispatcher_module.WorkflowRunReport:
        captured[name] = inputs
        return _report("ok")

    session: Any = _FakeSession()
    dispatcher = IntentDispatcher(
        session=session,
        known_tools=(),
        workflow_runner=_runner,
    )

    await dispatcher.dispatch(
        RouterResponse(
            intents=[
                Intent(
                    type=RUN_WORKFLOW_INTENT_TYPE,
                    args={"name": "weekly", "inputs": "not-a-mapping"},
                )
            ]
        )
    )

    assert captured == {"weekly": {}}


async def test_dispatcher_run_workflow_without_runner_soft_errors() -> None:
    """If no runner is wired the dispatcher still produces a soft result.

    Without the soft-fail path a ``run_workflow`` intent emitted by
    Haiku in a test or misconfigured runtime would crash the loop.
    """
    session: Any = _FakeSession()
    dispatcher = IntentDispatcher(session=session, known_tools=())

    results = await dispatcher.dispatch(
        RouterResponse(
            intents=[Intent(type=RUN_WORKFLOW_INTENT_TYPE, args={"name": "weekly"})]
        )
    )

    assert len(results) == 1
    assert "weekly" in results[0].body
    assert session.calls == []


async def test_dispatcher_run_workflow_missing_name_arg_soft_errors() -> None:
    """A malformed run_workflow intent (no ``name`` arg) soft-errors.

    Defensive: Haiku could emit the intent type without filling the
    expected ``args["name"]`` slot. The dispatcher must not raise and
    must not invoke the runner.
    """
    invoked: list[str] = []

    async def _runner(
        name: str, _inputs: dict[str, str]
    ) -> dispatcher_module.WorkflowRunReport:
        invoked.append(name)
        return _report("ok")

    session: Any = _FakeSession()
    dispatcher = IntentDispatcher(
        session=session,
        known_tools=(),
        workflow_runner=_runner,
    )

    results = await dispatcher.dispatch(
        RouterResponse(intents=[Intent(type=RUN_WORKFLOW_INTENT_TYPE)])
    )

    assert invoked == []
    assert len(results) == 1
    assert "workflow" in results[0].body.lower()


async def test_dispatcher_run_workflow_preserves_privacy_tier_ceiling() -> None:
    """A ``personal`` walk surfaces a ``personal`` result, not a downgraded one.

    #1152 changed *whose* ceiling the result echoes — the walk's, not
    the intent's — but not the fact that an admitted ceiling survives
    the trip. Both are ``personal`` here so the round-trip is the only
    thing under test;
    ``test_run_workflow_result_uses_walk_ceiling_not_intent_ceiling``
    pulls them apart.
    """

    async def _runner(
        _name: str, _inputs: dict[str, str]
    ) -> dispatcher_module.WorkflowRunReport:
        return _report("ok", PrivacyTierCeiling.PERSONAL)

    session: Any = _FakeSession()
    dispatcher = IntentDispatcher(
        session=session,
        known_tools=(),
        workflow_runner=_runner,
    )

    results = await dispatcher.dispatch(
        RouterResponse(
            intents=[
                Intent(
                    type=RUN_WORKFLOW_INTENT_TYPE,
                    privacy_tier_ceiling="personal",
                    args={"name": "weekly"},
                )
            ]
        )
    )

    assert results[0].privacy_tier_ceiling == "personal"


# ---------------------------------------------------------------------------
# Composer privacy-tier cap at the dispatch boundary (#1152)
# ---------------------------------------------------------------------------
#
# The Haiku router's ``privacy_tier_ceiling`` is untrusted: raw MCP tool
# bodies (vault fragments built from third-party ChatGPT / Discord /
# Substack exports) are rendered verbatim into the router prompt. Nothing
# downstream caps it either — CrawDad speaks MCP over stdio, so the
# server's ``admitted_ceiling`` sees a LOCAL caller and imposes no cap.
# The dispatcher is the chokepoint.


@pytest.mark.parametrize("requested", _ABOVE_CAP, ids=_ABOVE_CAP_IDS)
async def test_dispatch_never_forwards_a_ceiling_above_the_composer_cap(
    requested: PrivacyTierCeiling,
) -> None:
    """[AC1] An ``intimate`` / ``all`` intent reaches MCP as ``personal``.

    Clamped, not refused: refusing would let one poisoned vault fragment
    permanently DoS the bot, since ``AgentLoop.run`` catches only
    ``UnknownIntentError`` / ``MCPUnavailableError``.
    """
    session: Any = _FakeSession(replies={"creek.state.read": "ok"})
    dispatcher = IntentDispatcher(session=session, known_tools=("creek.state.read",))

    await dispatcher.dispatch(
        RouterResponse(
            intents=[Intent(type="creek.state.read", privacy_tier_ceiling=requested)]
        )
    )

    assert len(session.calls) == 1
    assert session.calls[0][1]["privacy_tier_ceiling"] == "personal"


@pytest.mark.parametrize("requested", _RANK_ORDER, ids=_RANK_ORDER_IDS)
async def test_dispatch_never_widens_any_requested_ceiling(
    requested: PrivacyTierCeiling,
) -> None:
    """The forwarded ceiling is admitted AND never ranks above the request.

    The ``open`` case is the point: a clamp mis-implemented as "always
    send personal" would satisfy every other test in this file and
    quietly *widen* the narrowest setting the operator can ask for.
    Together the two assertions say exactly what the security claim
    needs — capped from above, untouched from below.
    """
    session: Any = _FakeSession(replies={"creek.state.read": "ok"})
    dispatcher = IntentDispatcher(session=session, known_tools=("creek.state.read",))

    await dispatcher.dispatch(
        RouterResponse(
            intents=[Intent(type="creek.state.read", privacy_tier_ceiling=requested)]
        )
    )

    forwarded = session.calls[0][1]["privacy_tier_ceiling"]
    assert PrivacyTierCeiling(forwarded) in _ADMITTED
    assert _rank(forwarded) <= _rank(requested)


@pytest.mark.parametrize("requested", _ADMITTED, ids=_ADMITTED_IDS)
async def test_dispatch_forwards_admitted_ceilings_unchanged(
    requested: PrivacyTierCeiling,
) -> None:
    """An already-admitted ceiling crosses the boundary byte-identical.

    Guards the other direction from the cap: over-narrowing would make
    every ``personal`` request silently return ``open``-tier content,
    which reads as "the vault is empty" rather than as a refusal.
    """
    session: Any = _FakeSession(replies={"creek.state.read": "ok"})
    dispatcher = IntentDispatcher(session=session, known_tools=("creek.state.read",))

    await dispatcher.dispatch(
        RouterResponse(
            intents=[Intent(type="creek.state.read", privacy_tier_ceiling=requested)]
        )
    )

    assert session.calls[0][1]["privacy_tier_ceiling"] == requested.value


async def test_tool_result_carries_the_capped_ceiling_not_the_requested_one() -> None:
    """The ``ToolResult`` echoes what was sent, not what was asked for.

    ``loop._max_ceiling_from`` ratchets the auto-injected ``creek.save``
    up to the highest ceiling seen across results. If the result kept
    the *requested* tier, the cap would hold on this call and leak on
    the next one.
    """
    session: Any = _FakeSession(replies={"creek.state.read": "ok"})
    dispatcher = IntentDispatcher(session=session, known_tools=("creek.state.read",))

    results = await dispatcher.dispatch(
        RouterResponse(
            intents=[
                Intent(
                    type="creek.state.read",
                    privacy_tier_ceiling=PrivacyTierCeiling.ALL,
                )
            ]
        )
    )

    assert results[0].privacy_tier_ceiling is PrivacyTierCeiling.PERSONAL


async def test_activate_register_result_is_stamped_open() -> None:
    """A register switch is a fixed status line, so its result is ``open``.

    This branch makes no MCP call, so ``_build_arguments`` never runs —
    yet the result still feeds the ``_max_ceiling_from`` ratchet. An
    attacker could otherwise emit a harmless-looking
    ``crawdad.activate_register`` at ``all`` purely to raise the
    auto-injected ``creek.save``. The body never contains vault
    content, so ``open`` is the honest stamp.
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
        RouterResponse(
            intents=[
                Intent(
                    type=ACTIVATE_REGISTER_INTENT_TYPE,
                    privacy_tier_ceiling=PrivacyTierCeiling.ALL,
                    args={"register": "analytic"},
                )
            ]
        )
    )

    assert session.calls == []
    assert results[0].privacy_tier_ceiling is PrivacyTierCeiling.OPEN


async def test_run_workflow_soft_error_results_are_stamped_open() -> None:
    """No runner wired → a status-line body, so the result is ``open``.

    Nothing was read from the vault, so echoing the intent's declared
    ceiling would hand the ratchet a tier that no content justifies.
    """
    session: Any = _FakeSession()
    dispatcher = IntentDispatcher(session=session, known_tools=())

    results = await dispatcher.dispatch(
        RouterResponse(
            intents=[
                Intent(
                    type=RUN_WORKFLOW_INTENT_TYPE,
                    privacy_tier_ceiling=PrivacyTierCeiling.PERSONAL,
                    args={"name": "weekly"},
                )
            ]
        )
    )

    assert results[0].privacy_tier_ceiling is PrivacyTierCeiling.OPEN


async def test_run_workflow_missing_name_result_is_stamped_open() -> None:
    """The other soft-error branch (no ``name`` arg) is stamped ``open`` too.

    The runner is wired but must never be reached, so no walk happened
    and no vault content is in the body.
    """

    async def _unreached(
        _name: str, _inputs: dict[str, str]
    ) -> dispatcher_module.WorkflowRunReport:
        msg = "workflow runner reached despite a missing 'name' arg"
        raise AssertionError(msg)

    session: Any = _FakeSession()
    dispatcher = IntentDispatcher(
        session=session,
        known_tools=(),
        workflow_runner=_unreached,
    )

    results = await dispatcher.dispatch(
        RouterResponse(
            intents=[
                Intent(
                    type=RUN_WORKFLOW_INTENT_TYPE,
                    privacy_tier_ceiling=PrivacyTierCeiling.PERSONAL,
                )
            ]
        )
    )

    assert results[0].privacy_tier_ceiling is PrivacyTierCeiling.OPEN


async def test_run_workflow_result_uses_walk_ceiling_not_intent_ceiling() -> None:
    """The walk reports the ceiling it used; the intent's is discarded.

    The walker caps the workflow's *declared* ceiling itself, so the
    tier that actually selected the content is the runner's, not the
    router's. Router-declared ``personal`` over an ``open`` walk must
    not raise the result.
    """

    async def _runner(
        _name: str, _inputs: dict[str, str]
    ) -> dispatcher_module.WorkflowRunReport:
        return _report("walked", PrivacyTierCeiling.OPEN)

    session: Any = _FakeSession()
    dispatcher = IntentDispatcher(
        session=session,
        known_tools=(),
        workflow_runner=_runner,
    )

    results = await dispatcher.dispatch(
        RouterResponse(
            intents=[
                Intent(
                    type=RUN_WORKFLOW_INTENT_TYPE,
                    privacy_tier_ceiling=PrivacyTierCeiling.PERSONAL,
                    args={"name": "weekly"},
                )
            ]
        )
    )

    assert results[0].body == "walked"
    assert results[0].privacy_tier_ceiling is PrivacyTierCeiling.OPEN


async def test_dispatch_logs_a_warning_when_it_caps(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Capping is loud, names both tiers, and quotes no vault content.

    The #1090 hazard: a log line (or user-facing string) that
    interpolated a tool-result body would turn the cap into an oracle
    over content the caller was never admitted to. The record must name
    the intent type and the two tiers — module constants and a tool
    name — and nothing that came back over the wire.
    """
    session: Any = _FakeSession(replies={"creek.state.read": "VAULT-BODY-SENTINEL"})
    dispatcher = IntentDispatcher(session=session, known_tools=("creek.state.read",))

    with caplog.at_level(logging.WARNING, logger="crawdad.dispatcher"):
        await dispatcher.dispatch(
            RouterResponse(
                intents=[
                    Intent(
                        type="creek.state.read",
                        privacy_tier_ceiling=PrivacyTierCeiling.ALL,
                    )
                ]
            )
        )

    warnings = [
        record.getMessage()
        for record in caplog.records
        if record.levelno >= logging.WARNING
    ]
    assert len(warnings) == 1
    assert "creek.state.read" in warnings[0]
    # Word-boundary matches: a bare ``"all" in ...`` would also be
    # satisfied by the word "call" in a message that named no tier.
    assert re.search(r"\ball\b", warnings[0]) is not None
    assert re.search(r"\bpersonal\b", warnings[0]) is not None
    assert "VAULT-BODY-SENTINEL" not in warnings[0]


async def test_dispatch_does_not_log_a_warning_when_nothing_is_capped(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An admitted ceiling passes silently — the warning means something.

    Without this, "always warn" would satisfy the test above and drown
    the real signal in noise on every single tool call.
    """
    session: Any = _FakeSession(replies={"creek.state.read": "ok"})
    dispatcher = IntentDispatcher(session=session, known_tools=("creek.state.read",))

    with caplog.at_level(logging.WARNING, logger="crawdad.dispatcher"):
        await dispatcher.dispatch(
            RouterResponse(
                intents=[
                    Intent(
                        type="creek.state.read",
                        privacy_tier_ceiling=PrivacyTierCeiling.PERSONAL,
                    )
                ]
            )
        )

    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]

    assert warnings == []
