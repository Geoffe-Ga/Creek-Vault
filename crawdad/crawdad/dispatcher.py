"""Intent → MCP tool dispatcher (FEAT-014).

Takes a :class:`crawdad.intents.RouterResponse`, validates each intent
against the snapshot of MCP tool names taken at session start, and
invokes the matching tool with the intent's args. Tool results come
back as a list of :class:`ToolResult` envelopes that the bot handler
formats into a Discord reply (FEAT-014's "boring summary" path).

FEAT-015 wraps this dispatcher in the 5-round loop.

The dispatcher is also the chokepoint for the cloud-composer privacy cap
(#1152). The router's ``privacy_tier_ceiling`` used to be forwarded
verbatim on the theory that the MCP server enforced the policy at the
protocol boundary (FEAT-010/011) — but CrawDad speaks MCP over **stdio**,
which ``creek_mcp/server.py::_caller_identity`` classifies as a LOCAL
caller, so the server-side remote cap never fires here. Meanwhile the
router's ceiling is untrusted: raw tool-result bodies reach the router
prompt through the conversation history. Every intent therefore passes
through :func:`_capped` before any branch acts on it.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict

from crawdad.intents import (
    ACTIVATE_REGISTER_INTENT_TYPE,
    COMPOSER_ADMITTED_CEILINGS,
    RUN_WORKFLOW_INTENT_TYPE,
    PrivacyTierCeiling,
    cap_ceiling,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterable

    from crawdad.intents import Intent, RouterResponse
    from crawdad.mcp_client import MCPSession

    # FEAT-029: synchronous callable that swaps the active voice-skill
    # register. Returns True on success (register file present, stack
    # reloaded), False on unknown / unsafe register names. The loop owns
    # the underlying :class:`SkillStackRegistry` and passes its
    # ``activate_register`` method down to the dispatcher.
    RegisterSwitcher = Callable[[str], bool]

_LOGGER = logging.getLogger("crawdad.dispatcher")


class UnknownIntentError(RuntimeError):
    """Router emitted an intent whose ``type`` is not an advertised tool.

    The bot handler maps this to a user-facing "I tried to do X but I
    don't have a tool for it" reply.
    """


class ToolResult(BaseModel):
    """One tool's response, threaded back through the loop.

    ``privacy_tier_ceiling`` records the ceiling that actually selected
    this body, so downstream consumers (e.g. the loop's paradox-routing
    helper) can re-authorise follow-up tool calls at the same tier
    without rediscovering the source authorization.

    "Actually selected" is the #1152 correction: the stamp is the
    *capped* ceiling the wire call carried, never the one the router
    asked for, and branches that make no tool call at all stamp ``open``
    because their bodies are fixed status lines. ``loop._max_ceiling_from``
    ratchets the auto-injected ``creek.save`` up to the highest ceiling
    it sees here, so an over-generous stamp leaks on the *next* call even
    when the current one was capped correctly.
    """

    model_config = ConfigDict(frozen=True)

    intent_type: str
    body: str
    privacy_tier_ceiling: PrivacyTierCeiling = PrivacyTierCeiling.OPEN


class WorkflowRunReport(BaseModel):
    """What an ADAPT-003 workflow runner hands back to its caller.

    A bare reply string was not enough (#1152). The walker caps the
    workflow's *declared* ceiling itself, and its soft-error paths
    (unknown name, mid-run failure) read nothing from the vault at all,
    so the only component that knows which tier actually selected the
    returned text is the runner. Reporting it explicitly lets
    :meth:`IntentDispatcher._handle_run_workflow` stamp the truth
    instead of echoing the router's unrelated guess.

    ``privacy_tier_ceiling`` has no default on purpose: every runner has
    to state the tier its walk used, and a default would let a new
    soft-error branch quietly inherit somebody else's answer.
    """

    model_config = ConfigDict(frozen=True)

    reply: str
    privacy_tier_ceiling: PrivacyTierCeiling


if TYPE_CHECKING:
    # ADAPT-003: async callable that runs a named workflow end-to-end and
    # reports the composed reply plus the ceiling the walk used. Mirrors
    # the slash-command ``crawdad.slash_commands.WorkflowRunner`` shape so
    # the CLI can hand the same closure (built by
    # ``crawdad.cli._build_workflow_runner``) to both the slash-command
    # surface and the dispatcher. Receives the workflow ``name`` plus an
    # ``inputs`` mapping for ``{{input.<key>}}`` interpolation.
    #
    # Declared here rather than in the import block above only because it
    # names :class:`WorkflowRunReport`, which has to exist first.
    WorkflowDispatchRunner = Callable[
        [str, dict[str, str]], Awaitable[WorkflowRunReport]
    ]


class IntentDispatcher:
    """Forward each router intent to the corresponding MCP tool call."""

    def __init__(
        self,
        *,
        session: MCPSession,
        known_tools: Iterable[str],
        register_switcher: RegisterSwitcher | None = None,
        workflow_runner: WorkflowDispatchRunner | None = None,
    ) -> None:
        """Cache the advertised tools so unknown intents fail fast.

        Args:
            session: An already-connected
                :class:`crawdad.mcp_client.MCPSession`.
            known_tools: The MCP tool names snapshotted at session
                start. Intent types outside this set raise
                :class:`UnknownIntentError`.
            register_switcher: FEAT-029 callback that swaps the loop's
                active voice register. ``None`` causes
                ``crawdad.activate_register`` intents to soft-error
                rather than mutate the skill stack — useful in tests
                and when the registry isn't wired (no vault layout).
            workflow_runner: ADAPT-003 async callback that walks an
                authored workflow by name and returns a
                :class:`WorkflowRunReport` — the composed reply plus the
                ceiling that walk used. ``None`` causes
                ``crawdad.run_workflow`` intents to soft-error rather
                than crash — useful in tests and when the workflow
                surface isn't wired (no composer / no MCP tools
                advertised).
        """
        self._session = session
        self._known_tools = frozenset(known_tools)
        self._register_switcher = register_switcher
        self._workflow_runner = workflow_runner

    async def dispatch(self, response: RouterResponse) -> list[ToolResult]:
        """Invoke each intent in order; collect the textual tool results.

        Args:
            response: Router output to act on.

        Returns:
            One :class:`ToolResult` per intent, in document order.

        Raises:
            UnknownIntentError: an intent's ``type`` is not advertised.
            crawdad.mcp_client.MCPUnavailableError: forwarded from the
                MCP session; the bot handler maps it to the documented
                soft-error reply.
        """
        results: list[ToolResult] = []
        for raw_intent in response.intents:
            # #1152: cap at the head of the loop, before any branch sees
            # the intent. The two client-side branches make no MCP call
            # yet still stamp ``ToolResult.privacy_tier_ceiling``, which
            # feeds ``loop._max_ceiling_from``'s upward-only ratchet — so
            # an attacker could emit a ``crawdad.activate_register`` at
            # ``all`` purely to raise the auto-injected ``creek.save``.
            # Capping here means no present or future branch can forget.
            intent = _capped(raw_intent)
            if intent.type == ACTIVATE_REGISTER_INTENT_TYPE:
                results.append(self._handle_activate_register(intent))
                continue
            if intent.type == RUN_WORKFLOW_INTENT_TYPE:
                results.append(await self._handle_run_workflow(intent))
                continue
            if intent.type not in self._known_tools:
                msg = (
                    f"router emitted intent type {intent.type!r} which is not "
                    "in the advertised MCP tool set"
                )
                raise UnknownIntentError(msg)
            body = await self._session.call_tool(intent.type, _build_arguments(intent))
            _LOGGER.debug("dispatched %s: %d chars returned", intent.type, len(body))
            results.append(
                ToolResult(
                    intent_type=intent.type,
                    body=body,
                    privacy_tier_ceiling=intent.privacy_tier_ceiling,
                )
            )
        return results

    def _handle_activate_register(self, intent: Intent) -> ToolResult:
        """Run the FEAT-029 register switch for ``crawdad.activate_register``.

        The result body is a short status line the composer can quote
        verbatim (or summarise) for the user — a register name and fixed
        wording, never vault content. The result is therefore stamped
        ``open`` (#1152) rather than echoing the intent's ceiling: no
        content was read, so no tier is justified, and echoing one would
        hand ``loop._max_ceiling_from``'s ratchet a free upgrade for a
        call that touched nothing.
        """
        register = intent.args.get("register")
        if not isinstance(register, str) or not register:
            _LOGGER.info("activate_register intent missing 'register' arg")
            body = "could not switch voice register: no register name was provided"
        elif self._register_switcher is None:
            _LOGGER.info(
                "activate_register intent for %r but no switcher is wired", register
            )
            body = (
                f"voice register switching is unavailable in this session; "
                f"ignoring request to activate {register!r}"
            )
        elif self._register_switcher(register):
            _LOGGER.info("voice register switched to %r", register)
            body = f"voice register switched to {register!r}"
        else:
            _LOGGER.info("voice register switch refused for %r", register)
            body = f"unknown voice register {register!r}; keeping the current register"
        return _status_result(intent, body)

    async def _handle_run_workflow(self, intent: Intent) -> ToolResult:
        """Run the ADAPT-003 workflow walk for ``crawdad.run_workflow``.

        The result body is the composer's user-facing reply (or a short
        status line when the workflow surface is unavailable / the
        intent is malformed).

        Whose ceiling gets stamped is the #1152 question, and the answer
        is never the intent's. The two soft-error branches read nothing
        from the vault, so they stamp ``open``. The runner branch stamps
        :attr:`WorkflowRunReport.privacy_tier_ceiling` — the tier the
        *walk* used, which the walker capped against its own declared
        value and which has nothing to do with what the router guessed.

        Note that stamp may be *higher* than the (already capped)
        intent's ceiling: an ``open`` intent naming a ``personal``
        workflow yields a ``personal`` result, which ratchets the
        auto-injected ``creek.save`` to ``personal``. That is correct —
        the composed text really was selected at ``personal``, and the
        save has to be authorised for it — and it still cannot exceed
        the cap, because both the walker and the re-cap below bound it.
        """
        name = intent.args.get("name")
        if not isinstance(name, str) or not name:
            _LOGGER.info("run_workflow intent missing 'name' arg")
            return _status_result(
                intent, "could not run workflow: no workflow name was provided"
            )
        if self._workflow_runner is None:
            _LOGGER.info("run_workflow intent for %r but no runner is wired", name)
            return _status_result(
                intent,
                f"workflow running is unavailable in this session; "
                f"ignoring request to run {name!r}",
            )
        inputs = _extract_workflow_inputs(intent.args.get("inputs"))
        _LOGGER.info("running workflow %r with inputs %r", name, inputs)
        report = await self._workflow_runner(name, inputs)
        return ToolResult(
            intent_type=intent.type,
            body=report.reply,
            # Re-capped even though the only production runner
            # (``cli._build_workflow_runner``) already caps: this is the
            # one ceiling the dispatcher accepts from an injected
            # callable rather than deriving itself, and :func:`_capped`
            # claims to be the only gate on this path. Idempotent, so
            # the honest case is unaffected.
            privacy_tier_ceiling=cap_ceiling(report.privacy_tier_ceiling),
        )


def _capped(intent: Intent) -> Intent:
    """Return *intent* with its ceiling narrowed to the composer cap.

    This is the only gate on the path to ``session.call_tool``. It is
    deliberately NOT a pydantic validator on :class:`Intent`: as
    ``crawdad.workflows.WorkflowWalker._check_privacy_ceiling`` already
    records for the workflow cap, ``model_construct`` and
    ``model_copy(update=...)`` sail straight past validators, so a
    validator would hold only for the parsed-JSON construction route.
    A function on the dispatch path holds for all of them.

    Returns *intent* unchanged (same object) when its ceiling is already
    admitted, so the common case allocates nothing and the WARNING below
    keeps meaning something.

    The log line names the intent type and the two tiers and nothing
    else. Interpolating a tool-result body — or any vault-derived
    value — would turn the cap into an oracle over content the caller
    was never admitted to (the #1090 hazard).
    """
    requested = intent.privacy_tier_ceiling
    if requested in COMPOSER_ADMITTED_CEILINGS:
        return intent
    capped = cap_ceiling(requested)
    # ``%r`` on the type, matching the convention elsewhere in this
    # module: ``_capped`` runs BEFORE the ``known_tools`` membership
    # check, so ``intent.type`` here is an arbitrary router-emitted
    # string with no charset or length constraint. Unquoted, an
    # embedded newline would let a poisoned fragment forge log records
    # in the operator's log.
    _LOGGER.warning(
        "capped privacy_tier_ceiling for intent %r: requested %s, forwarding %s",
        intent.type,
        requested.value,
        capped.value,
    )
    return intent.model_copy(update={"privacy_tier_ceiling": capped})


def _status_result(intent: Intent, body: str) -> ToolResult:
    """Wrap a fixed status line as an ``open``-tier :class:`ToolResult`.

    Used by the client-side soft-error branches. The body is CrawDad's
    own wording plus a name the router supplied, so nothing was read
    from the vault and ``open`` — the most restrictive tier — is both
    the honest stamp and the one that cannot move the loop's ratchet.
    """
    return ToolResult(
        intent_type=intent.type,
        body=body,
        privacy_tier_ceiling=PrivacyTierCeiling.OPEN,
    )


def _extract_workflow_inputs(raw: object) -> dict[str, str]:
    """Coerce a router-supplied ``inputs`` arg into a ``{str: str}`` mapping.

    Haiku emits ``inputs`` as a free-form object; the workflow walker's
    ``{{input.<key>}}`` interpolation only handles string values, so we
    stringify every value and drop non-mapping payloads entirely. A
    missing or malformed ``inputs`` arg yields an empty dict rather than
    raising — required inputs are still enforced downstream by the
    walker's constraint check.
    """
    if not isinstance(raw, dict):
        return {}
    return {str(key): str(value) for key, value in raw.items()}


def _build_arguments(intent: Intent) -> dict[str, object]:
    """Merge the intent's args with the privacy_tier_ceiling field.

    The MCP server reads ``privacy_tier_ceiling`` from the call args as
    a sibling of any tool-specific arguments — see FEAT-010's tool
    signatures. The router carries it on the :class:`Intent` model;
    the intent model is authoritative, so we *overwrite* any
    conflicting key inside ``intent.args`` rather than letting Haiku
    smuggle a looser tier through the args dict.

    The field is trustworthy by the time it gets here only because
    :func:`_capped` ran at the head of :meth:`IntentDispatcher.dispatch`.
    """
    payload: dict[str, object] = dict(intent.args)
    payload["privacy_tier_ceiling"] = intent.privacy_tier_ceiling.value
    return payload
