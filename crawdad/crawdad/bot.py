"""Pure-logic Discord message handling.

``handle_message`` is intentionally a free function (not a ``Client``
method) so the test suite can drive it with fakes for ``Message``,
``Author``, and ``Channel``. The thin ``CrawDadClient`` subclass below
just forwards Discord events to the handler — every interesting
decision lives here.

FEAT-014 will swap the stub reply for the Haiku-router pipeline; the
allowlist gate and the missing-state / unreachable-MCP graceful
responses are stable.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Protocol, cast

import discord

if TYPE_CHECKING:
    from crawdad.config import CrawDadConfig
    from crawdad.mcp_client import MCPUnavailableError
    from crawdad.state import SessionState, StateUnavailableError

_LOGGER = logging.getLogger("crawdad.bot")

_STUB_REPLY = (
    "crawdad here — wiring scaffold is up. "
    "(FEAT-014 will swap this stub for a real Haiku-routed response.)"
)
_STATE_UNAVAILABLE_REPLY = (
    "no audit report yet — run `creek state` in the vault and try again."
)
_MCP_UNAVAILABLE_REPLY = "creek-tools is unreachable; try again in a moment."


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

    async def send(self, content: str) -> None: ...  # pragma: no cover - protocol stub


async def handle_message(
    message: _MessageLike,
    *,
    config: CrawDadConfig,
    session_state: SessionState | None,
    bot_user_id: int,
) -> None:
    """Process one Discord message.

    Args:
        message: The inbound Discord (or fake) message.
        config: Runtime config — supplies the allowlist.
        session_state: Result of :func:`crawdad.state.load_session_state`,
            or ``None`` if the load raised :class:`StateUnavailableError`.
        bot_user_id: The bot's own Discord user id. Used to suppress
            self-replies (which would otherwise feed back into
            ``on_message`` and loop).
    """
    if message.author.id == bot_user_id or message.author.bot:
        return
    if not config.is_allowed(user_id=message.author.id, channel_id=message.channel.id):
        return
    if session_state is None:
        await message.channel.send(_STATE_UNAVAILABLE_REPLY)
        return
    await message.channel.send(_STUB_REPLY)


def render_state_unavailable_reply(error: StateUnavailableError) -> str:
    """Map :class:`StateUnavailableError` to the user-facing message."""
    _LOGGER.info("state unavailable: %s", error)
    return _STATE_UNAVAILABLE_REPLY


def render_mcp_unavailable_reply(error: MCPUnavailableError) -> str:
    """Map :class:`MCPUnavailableError` to the user-facing message."""
    _LOGGER.warning("MCP unavailable: %s", error)
    return _MCP_UNAVAILABLE_REPLY


class CrawDadClient(discord.Client):
    """Tiny ``discord.Client`` subclass that delegates to :func:`handle_message`.

    The runtime wiring lives in :mod:`crawdad.cli`; this class is just
    the Discord glue.
    """

    def __init__(
        self,
        *,
        config: CrawDadConfig,
        session_state: SessionState | None,
        intents: discord.Intents | None = None,
    ) -> None:
        """Store config and session state; init the parent ``Client``."""
        super().__init__(intents=intents or self._default_intents())
        self._config = config
        self._session_state = session_state

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
            return
        await handle_message(
            cast("_MessageLike", message),
            config=self._config,
            session_state=self._session_state,
            bot_user_id=self.user.id,
        )
