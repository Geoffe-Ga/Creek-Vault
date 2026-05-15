"""Pure-logic Discord message handling (FEAT-013 + FEAT-014).

``handle_message`` is a free function so the test suite can drive it
with fakes for ``Message``, ``Author``, and ``Channel``. The thin
``CrawDadClient`` subclass forwards Discord events; every interesting
decision lives here.

FEAT-014 wires the Haiku router + MCP dispatcher into the handler. The
reply is still a "boring structured summary" — FEAT-015 swaps that for
the Sonnet composer.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Protocol, cast

import discord

from crawdad.dispatcher import IntentDispatcher, UnknownIntentError
from crawdad.history import ConversationHistory
from crawdad.mcp_client import MCPUnavailableError
from crawdad.router import RouterParseError

if TYPE_CHECKING:
    from crawdad.config import CrawDadConfig
    from crawdad.dispatcher import ToolResult
    from crawdad.mcp_client import MCPClient
    from crawdad.router import IntentRouter
    from crawdad.state import SessionState, StateUnavailableError

_LOGGER = logging.getLogger("crawdad.bot")

_STATE_UNAVAILABLE_REPLY = (
    "no audit report yet — run `creek state` in the vault and try again."
)
_MCP_UNAVAILABLE_REPLY = "creek-tools is unreachable; try again in a moment."
_ROUTER_PARSE_REPLY = "I lost the thread on that one — can you rephrase your question?"
_UNKNOWN_INTENT_REPLY = (
    "I tried to use a tool I don't have. Try a different question, or "
    "check `creek-tools` for the available tool surface."
)
_NO_TOOL_CALL_REPLY = (
    "noted — no tool call seemed necessary for that. (FEAT-015 will compose "
    "a richer reply.)"
)
_DISCORD_REPLY_LIMIT = 1900


class _MessageLike(Protocol):
    """Structural protocol covering the bits of ``discord.Message`` we use."""

    @property
    def author(self) -> _AuthorLike: ...  # pragma: no cover - protocol stub

    @property
    def channel(self) -> _ChannelLike: ...  # pragma: no cover - protocol stub

    @property
    def content(self) -> str: ...  # pragma: no cover - protocol stub


class _AuthorLike(Protocol):
    """Subset of ``discord.User`` / ``discord.Member`` used by the handler."""

    @property
    def id(self) -> int: ...  # pragma: no cover - protocol stub

    @property
    def bot(self) -> bool: ...  # pragma: no cover - protocol stub


class _ChannelLike(Protocol):
    """Subset of ``discord.abc.Messageable`` used by the handler."""

    @property
    def id(self) -> int: ...  # pragma: no cover - protocol stub

    async def send(self, content: str) -> None: ...  # pragma: no cover


async def handle_message(
    message: _MessageLike,
    *,
    config: CrawDadConfig,
    session_state: SessionState | None,
    bot_user_id: int,
    router: IntentRouter | None = None,
    mcp_client: MCPClient | None = None,
    known_tools: tuple[str, ...] = (),
    history: ConversationHistory | None = None,
) -> None:
    """Process one Discord message.

    Args:
        message: The inbound Discord (or fake) message.
        config: Runtime config — supplies the allowlist.
        session_state: Result of :func:`crawdad.state.load_session_state`,
            or ``None`` if missing.
        bot_user_id: The bot's own Discord user id (self-suppression).
        router: FEAT-014 Haiku router. ``None`` falls back to the
            FEAT-013 stub reply (used by tests that don't exercise the
            agent loop).
        mcp_client: Factory for opening an MCP session per message.
            Must be provided when ``router`` is.
        known_tools: MCP tool names snapshotted at session start;
            forwarded to :class:`IntentDispatcher` as the allowlist.
        history: Conversation transcript appended in place. ``None``
            means "don't record this turn" — handy for tests.
    """
    if message.author.id == bot_user_id or message.author.bot:
        return
    if not config.is_allowed(user_id=message.author.id, channel_id=message.channel.id):
        return
    if session_state is None:
        await message.channel.send(_STATE_UNAVAILABLE_REPLY)
        return
    if router is None or mcp_client is None:
        await message.channel.send(_stub_reply())
        return
    await _route_and_dispatch(
        message=message,
        session_state=session_state,
        router=router,
        mcp_client=mcp_client,
        known_tools=known_tools,
        history=history,
    )


async def _route_and_dispatch(
    *,
    message: _MessageLike,
    session_state: SessionState,
    router: IntentRouter,
    mcp_client: MCPClient,
    known_tools: tuple[str, ...],
    history: ConversationHistory | None,
) -> None:
    """Run the Haiku → dispatcher pipeline; format the reply."""
    if history is not None:
        history.append("user", message.content)
    try:
        response = await router.extract_intents(
            message=message.content,
            history=history or ConversationHistory(),
            state=session_state,
        )
    except RouterParseError as exc:
        _LOGGER.warning("router parse error: %s", exc)
        await message.channel.send(_ROUTER_PARSE_REPLY)
        return
    try:
        async with mcp_client.connect() as session:
            dispatcher = IntentDispatcher(session=session, known_tools=known_tools)
            results = await dispatcher.dispatch(response)
    except UnknownIntentError as exc:
        _LOGGER.warning("unknown intent: %s", exc)
        await message.channel.send(_UNKNOWN_INTENT_REPLY)
        return
    except MCPUnavailableError as exc:
        _LOGGER.warning("MCP unavailable mid-dispatch: %s", exc)
        await message.channel.send(_MCP_UNAVAILABLE_REPLY)
        return
    reply = format_structured_reply(results)
    if history is not None:
        history.append("assistant", reply)
    await message.channel.send(reply)


def format_structured_reply(results: list[ToolResult]) -> str:
    """Render dispatcher results as the FEAT-014 boring-summary text.

    FEAT-015 replaces this with the Sonnet composer; the structure here
    is deliberately bland and parseable so behaviour gaps stay visible
    until the composer lands.
    """
    if not results:
        return _NO_TOOL_CALL_REPLY
    lines = ["I called these tools:"]
    for result in results:
        snippet = result.body.strip().replace("\n", " ")
        if len(snippet) > 300:
            snippet = snippet[:297] + "..."
        lines.append(f"- `{result.intent_type}` → {snippet}")
    rendered = "\n".join(lines)
    if len(rendered) > _DISCORD_REPLY_LIMIT:
        rendered = rendered[: _DISCORD_REPLY_LIMIT - 3] + "..."
    return rendered


def render_state_unavailable_reply(error: StateUnavailableError) -> str:
    """Map :class:`StateUnavailableError` to the user-facing message."""
    _LOGGER.info("state unavailable: %s", error)
    return _STATE_UNAVAILABLE_REPLY


def render_mcp_unavailable_reply(error: MCPUnavailableError) -> str:
    """Map :class:`MCPUnavailableError` to the user-facing message."""
    _LOGGER.warning("MCP unavailable: %s", error)
    return _MCP_UNAVAILABLE_REPLY


def _stub_reply() -> str:
    """FEAT-013 fallback used when the router/dispatcher aren't wired."""
    return (
        "crawdad here — wiring scaffold is up. "
        "(FEAT-015 will swap this for the composer-driven reply.)"
    )


class CrawDadClient(discord.Client):
    """``discord.Client`` subclass that delegates to :func:`handle_message`."""

    def __init__(
        self,
        *,
        config: CrawDadConfig,
        session_state: SessionState | None,
        router: IntentRouter | None = None,
        mcp_client: MCPClient | None = None,
        known_tools: tuple[str, ...] = (),
        history: ConversationHistory | None = None,
        intents: discord.Intents | None = None,
    ) -> None:
        """Store config + agent-loop components; init the parent ``Client``."""
        super().__init__(intents=intents or self._default_intents())
        self._config = config
        self._session_state = session_state
        self._router = router
        self._mcp_client = mcp_client
        self._known_tools = known_tools
        self._history = history

    @staticmethod
    def _default_intents() -> discord.Intents:
        """Return the minimum intents the v1.0 bot needs."""
        intents = discord.Intents.default()
        intents.message_content = True
        return intents

    async def on_ready(self) -> None:
        """Log connection details. Real session-start work happens in CLI."""
        _LOGGER.info("crawdad connected as %s", self.user)

    async def on_message(self, message: discord.Message) -> None:
        """Forward Discord messages to :func:`handle_message`."""
        if self.user is None:
            _LOGGER.debug("on_message before client ready; dropping event")
            return
        await handle_message(
            cast("_MessageLike", message),
            config=self._config,
            session_state=self._session_state,
            bot_user_id=self.user.id,
            router=self._router,
            mcp_client=self._mcp_client,
            known_tools=self._known_tools,
            history=self._history,
        )
