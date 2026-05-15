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

from crawdad.intents import PrivacyTierCeiling

if TYPE_CHECKING:
    from collections.abc import Iterable

    from crawdad.intents import Intent, RouterResponse
    from crawdad.mcp_client import MCPSession

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
    ) -> None:
        """Cache the advertised tools so unknown intents fail fast.

        Args:
            session: An already-connected
                :class:`crawdad.mcp_client.MCPSession`.
            known_tools: The MCP tool names snapshotted at session
                start. Intent types outside this set raise
                :class:`UnknownIntentError`.
        """
        self._session = session
        self._known_tools = frozenset(known_tools)

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
