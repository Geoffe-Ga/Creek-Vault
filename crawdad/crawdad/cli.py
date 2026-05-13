"""``crawdad run`` entry point.

The CLI is intentionally small: parse argv, resolve config, hand off to
:func:`run_bot`. The runtime wires together :func:`load_session_state`,
:class:`MCPClient`, and :class:`CrawDadClient`.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from crawdad.bot import CrawDadClient
from crawdad.config import load_config
from crawdad.mcp_client import MCPClient, MCPUnavailableError
from crawdad.state import StateUnavailableError, load_session_state

if TYPE_CHECKING:
    from collections.abc import Sequence

    from crawdad.config import CrawDadConfig
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

    The MCP subprocess is connected on demand by FEAT-014's dispatcher;
    in this scaffold we just verify the connect/list_tools path during
    startup so a misconfigured argv surfaces immediately rather than on
    first message.
    """
    logging.basicConfig(level=logging.INFO)
    session_state = _safe_load_state(config.vault_path)
    asyncio.run(_probe_mcp(config))
    client = CrawDadClient(config=config, session_state=session_state)
    client.run(config.discord_bot_token)


def _safe_load_state(vault_path: Path) -> SessionState | None:
    """Load ``latest.md`` once; downgrade missing-file to ``None``."""
    try:
        return load_session_state(vault_path)
    except StateUnavailableError as exc:
        _LOGGER.warning("session state unavailable at startup: %s", exc)
        return None


async def _probe_mcp(config: CrawDadConfig) -> None:
    """Fail fast on a broken MCP argv; non-startup outages are handled later."""
    try:
        async with MCPClient(config.mcp_server_command).connect() as session:
            tools = await session.list_tools()
            _LOGGER.info("MCP tools advertised: %s", ", ".join(tools))
    except MCPUnavailableError as exc:
        _LOGGER.warning(
            "MCP probe failed at startup; the bot will start anyway: %s",
            exc,
        )


if __name__ == "__main__":  # pragma: no cover - executed via entry point
    main()
