"""Tests for ``crawdad.mcp_client``."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

from crawdad.mcp_client import MCPClient, MCPSession, MCPUnavailableError


def _fixture_command() -> tuple[str, ...]:
    """Return the argv that launches the fixture MCP stdio server."""
    tests_root = Path(__file__).resolve().parent
    return (
        sys.executable,
        "-c",
        (
            "import sys; "
            f"sys.path.insert(0, {str(tests_root)!r}); "
            "from fixtures.fake_mcp_server import main; main()"
        ),
    )


async def test_connect_exposes_list_tools() -> None:
    """``MCPClient.connect`` produces a session that lists the fixture tool."""
    async with MCPClient(_fixture_command()).connect() as session:
        tools = await session.list_tools()

    assert "echo" in tools


async def test_call_tool_round_trips() -> None:
    """``call_tool`` returns the structured result from the fixture server."""
    async with MCPClient(_fixture_command()).connect() as session:
        result = await session.call_tool("echo", {"message": "hello-crawdad"})

    assert "hello-crawdad" in result


async def test_connect_failure_raises_typed_error() -> None:
    """A bogus server command surfaces ``MCPUnavailableError``."""
    client = MCPClient(("python", "-c", "import sys; sys.exit(1)"))

    with pytest.raises(MCPUnavailableError):
        async with client.connect():
            pass


async def test_client_survives_subprocess_death() -> None:
    """A tool call after the server exits raises but does NOT crash the client."""
    # FEAT-013 acceptance criterion: subprocess dies mid-call, bot stays up.
    client = MCPClient(_fixture_command())
    async with client.connect() as session:
        # A tool name the fixture server does not register surfaces an error.
        with pytest.raises(MCPUnavailableError):
            await session.call_tool("__force_exit__", {})

    # The session context manager exited cleanly; the next connect succeeds.
    async with MCPClient(_fixture_command()).connect() as session:
        tools = await session.list_tools()
    assert "echo" in tools


def test_mcp_client_rejects_empty_argv() -> None:
    """An empty argv is a configuration error, caught at construction time."""
    with pytest.raises(ValueError, match="non-empty argv"):
        MCPClient(())


class _BrokenSession:
    """Fakes the parts of ``mcp.ClientSession`` we use, raising on every call."""

    async def list_tools(self) -> Any:
        msg = "subprocess gone"
        raise RuntimeError(msg)

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        msg = f"subprocess gone while calling {name}"
        raise RuntimeError(msg)


async def test_session_list_tools_translates_underlying_failure() -> None:
    """The adapter maps SDK errors to :class:`MCPUnavailableError`."""
    session = MCPSession(_BrokenSession())  # type: ignore[arg-type]

    with pytest.raises(MCPUnavailableError, match="list_tools"):
        await session.list_tools()


async def test_session_call_tool_translates_underlying_failure() -> None:
    """The adapter maps SDK errors to :class:`MCPUnavailableError`."""
    session = MCPSession(_BrokenSession())  # type: ignore[arg-type]

    with pytest.raises(MCPUnavailableError, match="call_tool"):
        await session.call_tool("anything", {})
