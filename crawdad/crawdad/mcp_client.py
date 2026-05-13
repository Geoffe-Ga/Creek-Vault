"""Async stdio wrapper around the Anthropic ``mcp`` SDK.

FEAT-013 §Pre-decided choices §35: "MCP transport: stdio (matches
FEAT-010's server). The MCP server runs as a subprocess of CrawDad in
v1.0."

The wrapper is deliberately small. It exposes a single
``connect()`` async-context-manager that yields a ``MCPSession`` adapter
with ``list_tools()`` and ``call_tool()``. Per-tool convenience methods
(``creek_save``, ``creek_state_read``, …) live in FEAT-014's
dispatcher, not here — the skeleton stays loop-agnostic.

Both ``connect`` failures and mid-session subprocess deaths surface as
:class:`MCPUnavailableError` so the bot's error path is one line.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


class MCPUnavailableError(RuntimeError):
    """The MCP subprocess could not be reached, started, or kept alive.

    FEAT-013 acceptance criterion: when the ``creek-tools-mcp``
    subprocess exits or stops responding, the bot does NOT exit. It
    catches this error and replies gracefully to Discord.
    """


class MCPSession:
    """A thin adapter over :class:`mcp.ClientSession`."""

    def __init__(self, session: ClientSession) -> None:
        """Wrap *session*; callers must use :meth:`MCPClient.connect`."""
        self._session = session

    async def list_tools(self) -> tuple[str, ...]:
        """Return the names of the tools the server advertises."""
        try:
            result = await self._session.list_tools()
        except Exception as exc:
            msg = "list_tools failed; the MCP subprocess may have exited"
            raise MCPUnavailableError(msg) from exc
        return tuple(tool.name for tool in result.tools)

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
    ) -> str:
        """Invoke *name* with *arguments* and return the concatenated text.

        FEAT-013 keeps the return type narrow on purpose: a single
        string suitable for embedding in a Discord reply. FEAT-014's
        dispatcher will introduce structured envelopes once the loop
        needs them.
        """
        try:
            result = await self._session.call_tool(name, arguments or {})
        except Exception as exc:
            msg = f"call_tool({name!r}) failed; the MCP subprocess may have exited"
            raise MCPUnavailableError(msg) from exc
        if result.isError:
            text = _text_payload(result.content)
            raise MCPUnavailableError(f"tool {name!r} returned an error: {text}")
        return _text_payload(result.content)


class MCPClient:
    """Configured factory for :class:`MCPSession` instances.

    The argv (``creek-tools-mcp`` by default) comes from
    :class:`crawdad.config.CrawDadConfig.mcp_server_command`.
    """

    def __init__(self, command: tuple[str, ...]) -> None:
        """Store *command* (the subprocess argv) for later ``connect`` calls."""
        if not command:
            msg = "MCP server command must be a non-empty argv tuple"
            raise ValueError(msg)
        self._command = command

    @asynccontextmanager
    async def connect(self) -> AsyncIterator[MCPSession]:
        """Spawn the MCP subprocess and yield an initialised session."""
        params = StdioServerParameters(
            command=self._command[0],
            args=list(self._command[1:]),
        )
        try:
            async with (
                stdio_client(params) as (read, write),
                ClientSession(read, write) as session,
            ):
                await session.initialize()
                yield MCPSession(session)
        except MCPUnavailableError:
            raise
        except Exception as exc:
            msg = (
                "could not start or connect to the MCP subprocess "
                f"({self._command[0]!r})"
            )
            raise MCPUnavailableError(msg) from exc


def _text_payload(content: list[Any]) -> str:
    """Concatenate every text fragment in *content* into a single string."""
    parts: list[str] = []
    for item in content:
        text = getattr(item, "text", None)
        if text is not None:
            parts.append(str(text))
    return "\n".join(parts)
