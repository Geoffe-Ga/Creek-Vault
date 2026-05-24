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
from crawdad.composer import SonnetComposer
from crawdad.config import (
    DEFAULT_COMPOSER_MODEL,
    DEFAULT_ROUTER_MODEL,
    MAX_LOOP_ROUNDS,
    load_config,
)
from crawdad.history import ConversationHistory
from crawdad.intents import ToolInfo
from crawdad.loop import run_one_turn
from crawdad.mcp_client import MCPClient, MCPUnavailableError
from crawdad.router import IntentRouter
from crawdad.skill_loader import SkillStackRegistry, load_skills_for_session
from crawdad.state import StateUnavailableError, load_session_state

if TYPE_CHECKING:
    from collections.abc import Sequence

    from crawdad.config import CrawDadConfig
    from crawdad.mcp_client import ToolDetails
    from crawdad.slash_commands import LoopRunner
    from crawdad.state import SessionState

_LOGGER = logging.getLogger("crawdad.cli")

# Discord's regular message + slash-follow-up text body is capped at 2000
# characters. Truncate well below the wire limit so the truncation marker
# always fits and we leave headroom for accidental whitespace.
_DISCORD_REPLY_LIMIT = 1900


def _truncate_for_discord(text: str) -> str:
    """Cap *text* at Discord's soft limit with an ellipsis marker if needed."""
    if len(text) <= _DISCORD_REPLY_LIMIT:
        return text
    return text[: _DISCORD_REPLY_LIMIT - 3] + "..."


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
    """Boot the Discord client; load session state + voice skills at start.

    Startup runs two short-lived event loops in sequence: ``asyncio.run``
    drives :func:`_startup_probe` (which spawns and tears down the MCP
    subprocess to verify connectivity and snapshot the advertised
    tools), then ``discord.Client.run`` spins up the permanent loop.
    Each user-message turn spawns its own MCP subprocess via
    :class:`crawdad.mcp_client.MCPClient` until a future FEAT upgrades
    the loop to share a long-lived connection.

    FEAT-015 adds the composer + voice-skill stack to the wiring. The
    skill stack is loaded once at session start from
    ``<vault>/creek-skills/`` per the FEAT-015 plan; FEAT-029 introduced
    the :class:`SkillStackRegistry` so the active register can be
    swapped mid-session via natural language or
    ``/crawdad register <name>`` without restarting the bot.
    """
    logging.basicConfig(level=logging.INFO)
    session_state = _safe_load_state(config.vault_path)
    tool_details = asyncio.run(_startup_probe(config))
    initial_skills = load_skills_for_session(
        vault_path=config.vault_path, state=session_state
    )
    skill_registry = SkillStackRegistry(
        stack=initial_skills, vault_path=config.vault_path, state=session_state
    )
    components = _build_agent_components(config=config, tool_details=tool_details)
    loop_runner = _build_loop_runner(
        components=components,
        session_state=session_state,
        skill_registry=skill_registry,
        max_rounds=config.max_loop_rounds,
    )
    client = CrawDadClient(
        config=config,
        session_state=session_state,
        router=components.router,
        composer=components.composer,
        mcp_client=components.mcp_client,
        known_tools=components.known_tools,
        history=components.history,
        skills=skill_registry.stack,
        skill_registry=skill_registry,
        loop_runner=loop_runner,
        register_switcher=skill_registry.activate_register,
    )
    client.run(config.discord_bot_token)


def _build_loop_runner(
    *,
    components: _AgentComponents,
    session_state: SessionState | None,
    skill_registry: SkillStackRegistry,
    max_rounds: int = MAX_LOOP_ROUNDS,
) -> LoopRunner | None:
    """Return a closure that runs one loop turn end-to-end.

    The returned callable is what :mod:`crawdad.slash_commands` invokes
    when a Discord user types `/crawdad <command>`. ``None`` when the
    agent loop isn't wired (no router → no slash commands).

    The closure captures the :class:`SkillStackRegistry` so FEAT-029
    register switches persist across turns within the same bot session,
    and the operator-configured ``max_rounds`` (FEAT-036) so slash-command
    turns honour the same cap as plain-message turns.
    """
    if components.router is None or components.composer is None:
        return None
    router = components.router
    composer = components.composer
    mcp_client = components.mcp_client
    known_tools = components.known_tools
    history = components.history

    async def _runner(message: str) -> str:
        """Drive one loop turn and return the user-facing reply text.

        Catches unexpected exceptions so a misbehaving SDK call (network
        timeout, panic in the loop) doesn't leave Discord showing
        "interaction failed" with no user-visible reason. The catch is
        an additional safety net — the loop's structured outcomes
        already cover every documented failure mode. Replies are also
        truncated to Discord's soft cap so a long composer output
        cannot raise ``HTTPException`` past the safety net at
        ``interaction.followup.send`` time.
        """
        try:
            outcome = await run_one_turn(
                message=message,
                router=router,
                composer=composer,
                mcp_client=mcp_client,
                known_tools=known_tools,
                history=history,
                session_state=session_state,
                skills=skill_registry.stack,
                skill_registry=skill_registry,
                max_rounds=max_rounds,
            )
            return _truncate_for_discord(outcome.reply)
        except Exception:
            _LOGGER.exception("agent loop crashed for slash command turn")
            return _truncate_for_discord(
                "something went wrong on my end — creek-tools may be "
                "unreachable. Try again in a moment."
            )

    return _runner


class _AgentComponents:
    """Bundle of long-lived per-session objects returned to ``run_bot``."""

    __slots__ = (
        "composer",
        "history",
        "known_tools",
        "mcp_client",
        "router",
    )

    def __init__(
        self,
        *,
        router: IntentRouter | None,
        composer: SonnetComposer | None,
        mcp_client: MCPClient,
        known_tools: tuple[str, ...],
        history: ConversationHistory,
    ) -> None:
        """Store the wired components for ``CrawDadClient`` construction."""
        self.router = router
        self.composer = composer
        self.mcp_client = mcp_client
        self.known_tools = known_tools
        self.history = history


def _build_agent_components(
    *,
    config: CrawDadConfig,
    tool_details: tuple[ToolDetails, ...],
) -> _AgentComponents:
    """Construct the long-lived router + composer + MCP client.

    Returns ``router=None`` and ``composer=None`` when no tools were
    advertised; the bot falls back to the FEAT-013 stub reply rather
    than asking the agent loop to route into an empty tool set.
    """
    mcp_client = MCPClient(config.mcp_server_command)
    known_tools = tuple(tool.name for tool in tool_details)
    history = ConversationHistory()
    if not tool_details:
        _LOGGER.warning("no MCP tools advertised; agent loop is disabled this session")
        return _AgentComponents(
            router=None,
            composer=None,
            mcp_client=mcp_client,
            known_tools=known_tools,
            history=history,
        )
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
    composer = SonnetComposer(
        anthropic_client=anthropic_client,
        model=DEFAULT_COMPOSER_MODEL,
    )
    return _AgentComponents(
        router=router,
        composer=composer,
        mcp_client=mcp_client,
        known_tools=known_tools,
        history=history,
    )


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
