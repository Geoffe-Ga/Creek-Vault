"""Intent → MCP tool dispatcher (FEAT-014).

Takes a :class:`crawdad.intents.RouterResponse`, validates each intent
against the snapshot of MCP tool names taken at session start, and
invokes the matching tool with the intent's args. Tool results come
back as a list of :class:`ToolResult` envelopes that the bot handler
formats into a Discord reply (FEAT-014's "boring summary" path).

FEAT-015 will wrap this dispatcher in the 5-round loop; the
``privacy_tier_ceiling`` field is forwarded verbatim because the MCP
server enforces the policy at the protocol boundary (FEAT-010/011).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict

from crawdad.intents import (
    ACTIVATE_REGISTER_INTENT_TYPE,
    RUN_WORKFLOW_INTENT_TYPE,
    PrivacyTierCeiling,
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

    # ADAPT-003: async callable that runs a named workflow end-to-end and
    # returns the composed user-facing reply. Mirrors the slash-command
    # ``crawdad.slash_commands.WorkflowRunner`` shape so the CLI can hand
    # the same closure (built by ``crawdad.cli._build_workflow_runner``)
    # to both the slash-command surface and the dispatcher. Receives the
    # workflow ``name`` plus an ``inputs`` mapping for ``{{input.<key>}}``
    # interpolation.
    WorkflowDispatchRunner = Callable[[str, dict[str, str]], Awaitable[str]]

_LOGGER = logging.getLogger("crawdad.dispatcher")


class UnknownIntentError(RuntimeError):
    """Router emitted an intent whose ``type`` is not an advertised tool.

    The bot handler maps this to a user-facing "I tried to do X but I
    don't have a tool for it" reply.
    """


class ToolResult(BaseModel):
    """One tool's response, threaded back through the loop.

    ``privacy_tier_ceiling`` mirrors the source intent's ceiling so
    downstream consumers (e.g. the loop's paradox-routing helper) can
    re-authorise follow-up tool calls at the same or higher tier
    without rediscovering the source authorization.
    """

    model_config = ConfigDict(frozen=True)

    intent_type: str
    body: str
    privacy_tier_ceiling: PrivacyTierCeiling = PrivacyTierCeiling.OPEN


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
                authored workflow by name and returns the composed
                reply. ``None`` causes ``crawdad.run_workflow`` intents
                to soft-error rather than crash — useful in tests and
                when the workflow surface isn't wired (no composer /
                no MCP tools advertised).
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
        for intent in response.intents:
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
        verbatim (or summarise) for the user. The privacy ceiling on the
        result mirrors the intent's declared ceiling so downstream
        consumers see a faithful echo of the router's authorisation.
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
        return ToolResult(
            intent_type=intent.type,
            body=body,
            privacy_tier_ceiling=intent.privacy_tier_ceiling,
        )

    async def _handle_run_workflow(self, intent: Intent) -> ToolResult:
        """Run the ADAPT-003 workflow walk for ``crawdad.run_workflow``.

        The result body is the composer's user-facing reply (or a short
        status line when the workflow surface is unavailable / the
        intent is malformed). The privacy ceiling on the result mirrors
        the intent's declared ceiling so downstream consumers see a
        faithful echo of the router's authorisation.
        """
        name = intent.args.get("name")
        if not isinstance(name, str) or not name:
            _LOGGER.info("run_workflow intent missing 'name' arg")
            body = "could not run workflow: no workflow name was provided"
        elif self._workflow_runner is None:
            _LOGGER.info("run_workflow intent for %r but no runner is wired", name)
            body = (
                f"workflow running is unavailable in this session; "
                f"ignoring request to run {name!r}"
            )
        else:
            inputs = _extract_workflow_inputs(intent.args.get("inputs"))
            _LOGGER.info("running workflow %r with inputs %r", name, inputs)
            body = await self._workflow_runner(name, inputs)
        return ToolResult(
            intent_type=intent.type,
            body=body,
            privacy_tier_ceiling=intent.privacy_tier_ceiling,
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
    """
    payload: dict[str, object] = dict(intent.args)
    payload["privacy_tier_ceiling"] = intent.privacy_tier_ceiling.value
    return payload
