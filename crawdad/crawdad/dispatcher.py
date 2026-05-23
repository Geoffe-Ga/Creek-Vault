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

from crawdad.intents import ACTIVATE_REGISTER_INTENT_TYPE, PrivacyTierCeiling

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

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
        """
        self._session = session
        self._known_tools = frozenset(known_tools)
        self._register_switcher = register_switcher

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
