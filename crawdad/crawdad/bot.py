"""Pure-logic Discord message handling (FEAT-013 → FEAT-015).

``handle_message`` is a free function so the test suite can drive it
with fakes for ``Message``, ``Author``, and ``Channel``. The thin
``CrawDadClient`` subclass forwards Discord events; every interesting
decision lives here.

FEAT-015 wires the full agent loop: the handler hands off to
:func:`crawdad.loop.run_one_turn`, which orchestrates router →
dispatcher → composer with a hard cap of
:data:`crawdad.config.MAX_LOOP_ROUNDS`. The handler's only
responsibilities are now the allowlist gate, the session-state
fallback, and posting the loop's reply.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Protocol, cast

import discord
from discord import app_commands

from crawdad.history import ConversationHistory
from crawdad.loop import run_one_turn
from crawdad.slash_commands import register as register_slash_commands

if TYPE_CHECKING:
    from crawdad.composer import SonnetComposer
    from crawdad.config import CrawDadConfig
    from crawdad.mcp_client import MCPClient, MCPUnavailableError
    from crawdad.router import IntentRouter
    from crawdad.skill_loader import VoiceSkillStack
    from crawdad.slash_commands import LoopRunner
    from crawdad.state import SessionState, StateUnavailableError

_LOGGER = logging.getLogger("crawdad.bot")

_STATE_UNAVAILABLE_REPLY = (
    "no audit report yet — run `creek state` in the vault and try again."
)
_MCP_UNAVAILABLE_REPLY = "creek-tools is unreachable; try again in a moment."
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
    composer: SonnetComposer | None = None,
    mcp_client: MCPClient | None = None,
    known_tools: tuple[str, ...] = (),
    history: ConversationHistory | None = None,
    skills: VoiceSkillStack | None = None,
) -> None:
    """Process one Discord message.

    Args:
        message: The inbound Discord (or fake) message.
        config: Runtime config — supplies the allowlist.
        session_state: Result of :func:`crawdad.state.load_session_state`,
            or ``None`` if missing.
        bot_user_id: The bot's own Discord user id (self-suppression).
        router: FEAT-014 Haiku router.
        composer: FEAT-015 Sonnet composer.
        mcp_client: Factory for opening an MCP session per message.
        known_tools: MCP tool names snapshotted at session start.
        history: Conversation transcript appended in place.
        skills: Voice-skill stack loaded at session start.

    When ``router``, ``composer``, or ``mcp_client`` is ``None``, the
    handler falls back to the FEAT-013 stub reply — useful for tests
    that don't exercise the full loop.
    """
    if message.author.id == bot_user_id or message.author.bot:
        return
    if not config.is_allowed(user_id=message.author.id, channel_id=message.channel.id):
        return
    if session_state is None:
        await message.channel.send(_STATE_UNAVAILABLE_REPLY)
        return
    if router is None or composer is None or mcp_client is None:
        await message.channel.send(_stub_reply())
        return

    outcome = await run_one_turn(
        message=message.content,
        router=router,
        composer=composer,
        mcp_client=mcp_client,
        known_tools=known_tools,
        history=history or ConversationHistory(),
        session_state=session_state,
        skills=skills or _empty_skills(),
    )
    _LOGGER.info("loop outcome: %s", outcome.kind)
    await message.channel.send(_truncate_for_discord(outcome.reply))


def _empty_skills() -> VoiceSkillStack:
    """Return an empty :class:`VoiceSkillStack` for callers that opt out."""
    from crawdad.skill_loader import VoiceSkillStack

    return VoiceSkillStack(skills=())


def _truncate_for_discord(text: str) -> str:
    """Cap reply at Discord's soft limit so very long composer outputs land."""
    if len(text) <= _DISCORD_REPLY_LIMIT:
        return text
    return text[: _DISCORD_REPLY_LIMIT - 3] + "..."


def render_state_unavailable_reply(error: StateUnavailableError) -> str:
    """Map :class:`StateUnavailableError` to the user-facing message."""
    _LOGGER.info("state unavailable: %s", error)
    return _STATE_UNAVAILABLE_REPLY


def render_mcp_unavailable_reply(error: MCPUnavailableError) -> str:
    """Map :class:`MCPUnavailableError` to the user-facing message."""
    _LOGGER.warning("MCP unavailable: %s", error)
    return _MCP_UNAVAILABLE_REPLY


def _stub_reply() -> str:
    """Fallback used when the loop components aren't wired (test-only path)."""
    return (
        "crawdad here — wiring scaffold is up. "
        "(Agent loop components not configured this session.)"
    )


class CrawDadClient(discord.Client):
    """``discord.Client`` subclass that delegates to :func:`handle_message`."""

    def __init__(
        self,
        *,
        config: CrawDadConfig,
        session_state: SessionState | None,
        router: IntentRouter | None = None,
        composer: SonnetComposer | None = None,
        mcp_client: MCPClient | None = None,
        known_tools: tuple[str, ...] = (),
        history: ConversationHistory | None = None,
        skills: VoiceSkillStack | None = None,
        loop_runner: LoopRunner | None = None,
        intents: discord.Intents | None = None,
    ) -> None:
        """Store config + agent-loop components; init the parent ``Client``.

        ``loop_runner`` (FEAT-016) is the closure the slash command
        callbacks invoke to enter the FEAT-015 loop with a pre-baked
        user message. When ``None``, slash commands are not registered.
        """
        super().__init__(intents=intents or self._default_intents())
        self._config = config
        self._session_state = session_state
        self._router = router
        self._composer = composer
        self._mcp_client = mcp_client
        self._known_tools = known_tools
        self._history = history
        self._skills = skills
        self._loop_runner = loop_runner
        self.tree = app_commands.CommandTree(self)
        if loop_runner is not None:
            # discord.py's ``CommandTree.command`` signature is wider than
            # the ``_TreeLike`` Protocol the registration function uses,
            # so cast to Any at the boundary. Verified by the structural
            # ``test_register_wires_every_command_onto_tree`` test.
            register_slash_commands(cast("Any", self.tree), loop_runner=loop_runner)

    @staticmethod
    def _default_intents() -> discord.Intents:
        """Return the minimum intents the v1.0 bot needs."""
        intents = discord.Intents.default()
        intents.message_content = True
        return intents

    async def setup_hook(self) -> None:
        """Sync slash commands with Discord once the client is ready.

        ``setup_hook`` runs after login but before the gateway is
        ready, which is the documented home for ``CommandTree.sync()``.
        Skipped when the loop runner wasn't provided (test wiring).
        """
        if self._loop_runner is not None:
            await self.tree.sync()
            _LOGGER.info("synced /crawdad slash commands with Discord")

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
            composer=self._composer,
            mcp_client=self._mcp_client,
            known_tools=self._known_tools,
            history=self._history,
            skills=self._skills,
        )
