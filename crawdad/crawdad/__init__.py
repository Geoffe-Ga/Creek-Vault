"""CrawDad — Discord bot consuming the creek-tools MCP surface (FEAT-013).

The package exposes:

* :class:`crawdad.config.CrawDadConfig` — Pydantic settings, secrets via env.
* :func:`crawdad.state.load_session_state` — read the audit report once
  at session start.
* :class:`crawdad.mcp_client.MCPClient` — async stdio wrapper over the
  Anthropic ``mcp`` SDK.
* :func:`crawdad.bot.handle_message` — pure-logic message handler the
  ``discord.py`` client delegates to (kept side-effect-free for tests).
* :func:`crawdad.cli.main` — the ``crawdad run`` entry point.

FEAT-014 and FEAT-015 layer the Haiku-router + Sonnet-composer loop on
top of this skeleton.
"""

from crawdad.config import CrawDadConfig, load_config
from crawdad.state import SessionState, StateUnavailableError, load_session_state

__all__ = [
    "CrawDadConfig",
    "SessionState",
    "StateUnavailableError",
    "load_config",
    "load_session_state",
]
