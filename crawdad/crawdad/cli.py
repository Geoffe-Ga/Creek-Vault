"""``crawdad run`` entry point.

The CLI is intentionally small: parse argv, resolve config, hand off to
:func:`run_bot`. The runtime wires together :func:`load_session_state`,
:class:`MCPClient`, an :class:`IntentRouter`, and
:class:`CrawDadClient`.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING

import anthropic

from crawdad.bot import CrawDadClient
from crawdad.config import DEFAULT_ROUTER_MODEL, load_config
from crawdad.history import ConversationHistory
from crawdad.intents import ToolInfo
from crawdad.mcp_client import MCPClient, MCPUnavailableError
from crawdad.router import IntentRouter
from crawdad.state import StateUnavailableError, load_session_state

if TYPE_CHECKING:
    from collections.abc import Sequence

    from crawdad.config import CrawDadConfig
    from crawdad.mcp_client import ToolDetails
    from crawdad.state import SessionState

_LOGGER = logging.getLogger("crawdad.cli")


def main(argv: Sequence[str] | None = None) -> None:
    """Parse *argv*, resolve config, and start the runtime."""
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.subcommand == "run":
        config = load_config(args.config)
        run_bot(config)


def _build_parser() -> argparse.ArgumentParser:
    """Return the top-level argument parser."""
    parser = argparse.ArgumentParser(prog="crawdad")
    sub = parser.add_subparsers(dest="subcommand", required=True)
    run = sub.add_parser("run", help="Start the Discord bot.")
    run.add_argument(
        "--config",
        type=Path,
        default=None,
        help="Path to crawdad.yaml (defaults to ./crawdad.yaml).",
    )
    return parser


def run_bot(config: CrawDadConfig) -> None:
    """Boot the Discord client; load session state once at start.

    Startup runs two short-lived event loops in sequence: ``asyncio.run``
    drives :func:`_startup_probe` (which spawns and tears down the MCP
    subprocess to verify connectivity and snapshot the advertised
    tools), then ``discord.Client.run`` spins up the permanent loop.
    Each user-message turn spawns its own MCP subprocess via
    :class:`crawdad.mcp_client.MCPClient` until FEAT-014's loop is
    upgraded to share a long-lived connection.
    """
    logging.basicConfig(level=logging.INFO)
    session_state = _safe_load_state(config.vault_path)
    tool_details = asyncio.run(_startup_probe(config))
    router, mcp_client, known_tools, history = _build_agent_components(
        config=config, tool_details=tool_details
    )
    client = CrawDadClient(
        config=config,
        session_state=session_state,
        router=router,
        mcp_client=mcp_client,
        known_tools=known_tools,
        history=history,
    )
    client.run(config.discord_bot_token)


def _build_agent_components(
    *,
    config: CrawDadConfig,
    tool_details: tuple[ToolDetails, ...],
) -> tuple[IntentRouter | None, MCPClient, tuple[str, ...], ConversationHistory]:
    """Construct the long-lived router + MCP client + history for the session.

    Returns ``router=None`` when no tools were advertised (the probe
    failed); the bot falls back to the FEAT-013 stub reply rather than
    asking Haiku to route into an empty tool set.
    """
    mcp_client = MCPClient(config.mcp_server_command)
    known_tools = tuple(tool.name for tool in tool_details)
    history = ConversationHistory()
    if not tool_details:
        _LOGGER.warning("no MCP tools advertised; router is disabled this session")
        return None, mcp_client, known_tools, history
    anthropic_client = anthropic.AsyncAnthropic(api_key=config.anthropic_api_key)
    router_tools = [
        ToolInfo(
            name=tool.name,
            description=tool.description,
            input_schema=tool.input_schema,
        )
        for tool in tool_details
    ]
    router = IntentRouter(
        anthropic_client=anthropic_client,
        model=DEFAULT_ROUTER_MODEL,
        tools=router_tools,
    )
    return router, mcp_client, known_tools, history


def _safe_load_state(vault_path: Path) -> SessionState | None:
    """Load ``latest.md`` once; downgrade missing-file to ``None``."""
    try:
        return load_session_state(vault_path)
    except StateUnavailableError as exc:
        _LOGGER.warning("session state unavailable at startup: %s", exc)
        return None


async def _startup_probe(config: CrawDadConfig) -> tuple[ToolDetails, ...]:
    """Connectivity check + tool-surface snapshot.

    Calls :meth:`MCPSession.list_tool_details` so FEAT-014's router can
    build its intents schema and the dispatcher can refuse intents
    referencing unadvertised tool names. A misconfigured
    ``mcp_server_command`` that starts but advertises no tools yields
    an empty tuple — the caller sees that and disables the router for
    the session (still starts the bot, still posts the FEAT-013 stub).

    Probe failure is logged at WARNING and swallowed so the bot still
    starts; mid-session outages are handled by ``MCPUnavailableError``
    in :mod:`crawdad.bot`.
    """
    try:
        async with MCPClient(config.mcp_server_command).connect() as session:
            details = await session.list_tool_details()
            _LOGGER.info(
                "MCP tools advertised: %s",
                ", ".join(tool.name for tool in details),
            )
            return details
    except MCPUnavailableError as exc:
        _LOGGER.warning(
            "MCP probe failed at startup; the bot will start anyway: %s",
            exc,
        )
        return ()


if __name__ == "__main__":  # pragma: no cover - executed via entry point
    main()
