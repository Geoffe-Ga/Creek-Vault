"""Five-round agent loop (FEAT-015).

Orchestrates the FEAT-014 router + dispatcher around the FEAT-015
Sonnet composer. The loop's contract:

::

    user message
       │
       ▼  ── ROUND N ──┐
    router.extract_intents
       │
       ├─ compose=True or intents=[] ─► composer.compose ─► reply
       │
       └─ intents non-empty
              │
              ▼
          open MCP session
              │
              ▼
          dispatcher.dispatch(intents)  →  aggregate ToolResults
              │
              ▼
       (loop until MAX_LOOP_ROUNDS rounds; the 6th attempt is refused)

Side-effects: the loop appends ``user`` and ``assistant`` turns to the
shared :class:`ConversationHistory` (the source the next router pass
consumes). On the 6th-round refusal the history is cleared per FEAT-015
§pre-decided choice §27.

Paradox routing (§31): if any tool result mentions a paradox AND the
advertised tool set includes ``creek.save``, the loop injects a
``creek.save`` call (target=paradox) so the surfaced contradiction is
routed to ``10-Liminal/Paradoxes/`` before the composer sees it. When
``creek.save`` is not advertised (FEAT-011 not yet merged, etc.) the
loop still composes — the composer's prompt carries the paradox-name-
no-resolution rule independently.

Each tool-call round opens a fresh MCP session (per the FEAT-013
per-message lifecycle). A long-lived session is FEAT-016 territory.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict

from crawdad.composer import ComposerFailureError, mentions_paradox
from crawdad.config import MAX_LOOP_ROUNDS
from crawdad.dispatcher import (
    IntentDispatcher,
    ToolResult,
    UnknownIntentError,
)
from crawdad.intents import Intent, PrivacyTierCeiling, RouterResponse
from crawdad.mcp_client import MCPUnavailableError
from crawdad.router import RouterParseError

if TYPE_CHECKING:
    from crawdad.composer import SonnetComposer
    from crawdad.history import ConversationHistory
    from crawdad.mcp_client import MCPClient
    from crawdad.router import IntentRouter
    from crawdad.skill_loader import VoiceSkillStack
    from crawdad.state import SessionState

_LOGGER = logging.getLogger("crawdad.loop")

_TOO_DEEP_REPLY = (
    "I went too deep on this — let's back up. Can you reframe what you're looking for?"
)
_ROUTER_PARSE_REPLY = "I lost the thread on that one — can you rephrase your question?"
_UNKNOWN_INTENT_REPLY = (
    "I tried to use a tool I don't have. Try a different question, or check "
    "`creek-tools` for the available tool surface."
)
_MCP_UNAVAILABLE_REPLY = "creek-tools is unreachable; try again in a moment."
_COMPOSER_FAILURE_REPLY = (
    "I'm having trouble composing right now — try again in a moment."
)

_SAVE_TOOL_NAME = "creek.save"


OutcomeKind = Literal[
    "composed",
    "too_deep",
    "router_parse_error",
    "unknown_intent",
    "mcp_unavailable",
    "composer_failure",
]


class LoopOutcome(BaseModel):
    """The user-facing result of one loop run.

    The bot handler reads ``reply`` and posts it to Discord. ``kind``
    is logged so operators can tell apart "the loop worked" from "the
    loop fell back to a soft error message".
    """

    model_config = ConfigDict(frozen=True)

    kind: OutcomeKind
    reply: str


class AgentLoop:
    """Run-once orchestrator for the router/dispatch/compose pipeline."""

    def __init__(
        self,
        *,
        router: IntentRouter,
        composer: SonnetComposer,
        mcp_client: MCPClient,
        known_tools: tuple[str, ...],
        history: ConversationHistory,
        session_state: SessionState | None,
        skills: VoiceSkillStack,
        max_rounds: int = MAX_LOOP_ROUNDS,
    ) -> None:
        """Cache injected components for the per-turn :meth:`run` call."""
        self._router = router
        self._composer = composer
        self._mcp_client = mcp_client
        self._known_tools = tuple(known_tools)
        self._history = history
        self._session_state = session_state
        self._skills = skills
        self._max_rounds = max_rounds

    async def run(self, message: str) -> LoopOutcome:
        """Execute one user-turn loop end-to-end."""
        self._history.append("user", message)
        aggregated: list[ToolResult] = []

        for _round in range(self._max_rounds):
            try:
                response = await self._router.extract_intents(
                    message=message,
                    history=self._history,
                    state=self._session_state,
                )
            except RouterParseError as exc:
                _LOGGER.warning("router parse error mid-loop: %s", exc)
                return LoopOutcome(kind="router_parse_error", reply=_ROUTER_PARSE_REPLY)

            if response.compose or not response.intents:
                break

            try:
                results = await self._dispatch_round(response)
            except UnknownIntentError as exc:
                _LOGGER.warning("unknown intent mid-loop: %s", exc)
                return LoopOutcome(kind="unknown_intent", reply=_UNKNOWN_INTENT_REPLY)
            except MCPUnavailableError as exc:
                _LOGGER.warning("MCP unavailable mid-loop: %s", exc)
                return LoopOutcome(kind="mcp_unavailable", reply=_MCP_UNAVAILABLE_REPLY)
            aggregated.extend(results)
        else:
            # Exhausted max_rounds without the router setting compose=true.
            _LOGGER.warning(
                "agent loop exceeded %d rounds without compose=true; "
                "resetting session history",
                self._max_rounds,
            )
            self._history.clear()
            return LoopOutcome(kind="too_deep", reply=_TOO_DEEP_REPLY)

        try:
            aggregated = await self._maybe_route_paradox(aggregated)
        except MCPUnavailableError as exc:
            _LOGGER.warning("MCP unavailable during paradox save: %s", exc)
            return LoopOutcome(kind="mcp_unavailable", reply=_MCP_UNAVAILABLE_REPLY)

        try:
            reply = await self._composer.compose(
                user_message=message,
                tool_results=aggregated,
                history=self._history,
                state=self._session_state,
                skills=self._skills,
            )
        except ComposerFailureError as exc:
            _LOGGER.warning("composer failed: %s", exc)
            return LoopOutcome(kind="composer_failure", reply=_COMPOSER_FAILURE_REPLY)

        self._history.append("assistant", reply)
        return LoopOutcome(kind="composed", reply=reply)

    async def _dispatch_round(self, response: RouterResponse) -> list[ToolResult]:
        """Open one MCP session, dispatch this round's intents, return results."""
        async with self._mcp_client.connect() as session:
            dispatcher = IntentDispatcher(
                session=session, known_tools=self._known_tools
            )
            return await dispatcher.dispatch(response)

    async def _maybe_route_paradox(self, results: list[ToolResult]) -> list[ToolResult]:
        """If results mention a paradox, ensure a save call to liminal land.

        The auto-injected ``creek.save`` inherits the highest
        ``privacy_tier_ceiling`` seen across the surfacing results so
        the save call is authorised for whatever tier the source content
        sat at. Using a fixed OPEN ceiling would silently fail when a
        paradox surfaced inside personal- or intimate-tier content.

        Returns the (possibly augmented) results list. Idempotent — if
        a ``creek.save`` was already dispatched in this turn, no extra
        call is made.
        """
        if not mentions_paradox(results):
            return results
        if _SAVE_TOOL_NAME not in self._known_tools:
            _LOGGER.debug(
                "paradox surfaced but %s not advertised; composer handles it",
                _SAVE_TOOL_NAME,
            )
            return results
        already_saved = any(r.intent_type == _SAVE_TOOL_NAME for r in results)
        if already_saved:
            return results

        save_intent = Intent(
            type=_SAVE_TOOL_NAME,
            privacy_tier_ceiling=_max_ceiling_from(results),
            args={"target": "paradox"},
        )
        save_response = RouterResponse(intents=[save_intent], compose=False)
        try:
            saved = await self._dispatch_round(save_response)
        except UnknownIntentError:
            # Race: tool surface disagreed at the last moment. Just skip.
            _LOGGER.debug("creek.save dispatch race; skipping paradox routing")
            return results
        return [*results, *saved]


def _max_ceiling_from(results: list[ToolResult]) -> PrivacyTierCeiling:
    """Return the most permissive ceiling observed across *results*.

    Ordered low→high by the underlying enum value strings: ``all`` is
    treated as the most permissive (it's literally the "no ceiling"
    sentinel), then ``intimate`` → ``personal`` → ``open``. Defaults
    to OPEN when no results carry a ceiling.
    """
    rank = {
        PrivacyTierCeiling.OPEN: 0,
        PrivacyTierCeiling.PERSONAL: 1,
        PrivacyTierCeiling.INTIMATE: 2,
        PrivacyTierCeiling.ALL: 3,
    }
    best = PrivacyTierCeiling.OPEN
    for result in results:
        if rank[result.privacy_tier_ceiling] > rank[best]:
            best = result.privacy_tier_ceiling
    return best


async def run_one_turn(
    *,
    message: str,
    router: IntentRouter,
    composer: SonnetComposer,
    mcp_client: MCPClient,
    known_tools: tuple[str, ...],
    history: ConversationHistory,
    session_state: SessionState | None,
    skills: VoiceSkillStack,
) -> LoopOutcome:
    """Convenience wrapper the bot handler calls.

    Constructs an :class:`AgentLoop` from the per-session components and
    runs it for one user turn. Returns the structured :class:`LoopOutcome`
    so callers can branch on ``kind`` for logging / metrics.
    """
    loop = AgentLoop(
        router=router,
        composer=composer,
        mcp_client=mcp_client,
        known_tools=known_tools,
        history=history,
        session_state=session_state,
        skills=skills,
    )
    return await loop.run(message)
