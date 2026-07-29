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

import logging
import os
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any, Final

from mcp import ClientSession
from mcp.client.stdio import (
    StdioServerParameters,
    get_default_environment,
    stdio_client,
)
from pydantic import BaseModel, ConfigDict

from crawdad.config import _PROVIDER_KEY_ENV

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Mapping

_LOGGER = logging.getLogger("crawdad.mcp_client")

#: Credential env vars the stdio SDK scrubs but the in-MCP cloud tools need.
#: The provider API keys are derived from :data:`crawdad.config._PROVIDER_KEY_ENV`
#: so adding a provider there forwards its key automatically — issue #918 exists
#: because #610 added openai/gemini and this tuple was left Anthropic-only.
#: Two consent gates follow: ``CREEK_CLOUD_CONSENT`` is the canonical,
#: provider-neutral name and ``CREEK_ANTHROPIC_CONSENT`` its deprecated
#: back-compat alias. Both are spelled as literals here — they mirror
#: creek-tools' ``creek/classify/llm/consent.py`` rather than importing it,
#: because CrawDad must not take a Python dependency on creek-tools. Consent is
#: opt-in: each name is forwarded only when explicitly set in CrawDad's own
#: environment. See issues #549 and #918.
_FORWARDED_ENV_VARS: Final[tuple[str, ...]] = (
    *_PROVIDER_KEY_ENV.values(),
    "CREEK_CLOUD_CONSENT",
    "CREEK_ANTHROPIC_CONSENT",
)


def _subprocess_env(source: Mapping[str, str]) -> dict[str, str]:
    """Build the environment for the spawned ``creek-tools-mcp`` subprocess.

    The stdio SDK launches the server with a scrubbed environment limited to
    a small safe allowlist (:func:`get_default_environment`). That allowlist
    drops both the provider API keys and the cloud-consent vars, so an
    in-MCP tool that would otherwise reach a cloud provider silently
    degrades to non-cloud behaviour even though the operator consented —
    issues #549 and #918. Re-add the :data:`_FORWARDED_ENV_VARS` that are
    present (and non-empty) in *source* on top of the safe defaults.

    Forwarding consent is *not* a tier decision. Which content may reach a
    cloud provider stays the MCP server's call: creek-tools routes through
    its own ``ModelRouter`` chokepoint, which keeps Intimate-tier content
    local regardless of what this function forwards.

    Forwarding is opt-in in both directions: only names actually set in
    *source* are re-added, and nothing outside the allowlist is ever copied.

    Args:
        source: The parent environment to forward credentials from, normally
            :data:`os.environ`.

    Returns:
        The SDK safe-default environment augmented with any forwarded
        credentials. Absent or blank values are omitted entirely so the
        subprocess never sees an empty value.
    """
    env = dict(get_default_environment())
    for name in _FORWARDED_ENV_VARS:
        value = source.get(name)
        if value:
            env[name] = value
    return env


class MCPUnavailableError(RuntimeError):
    """The MCP subprocess could not be reached, started, or kept alive.

    FEAT-013 acceptance criterion: when the ``creek-tools-mcp``
    subprocess exits or stops responding, the bot does NOT exit. It
    catches this error and replies gracefully to Discord.
    """


class ToolDetails(BaseModel):
    """Normalised view of one tool from MCP ``tools/list``.

    FEAT-014's router uses these to build the intents JSON Schema.
    Kept here (not in :mod:`crawdad.intents`) so the wrapper module
    owns the SDK ↔ Pydantic translation in one place.
    """

    model_config = ConfigDict(frozen=True)

    name: str
    description: str
    input_schema: dict[str, Any]


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
            _LOGGER.debug("list_tools raised %s: %r", type(exc).__name__, exc)
            msg = "list_tools failed; the MCP subprocess may have exited"
            raise MCPUnavailableError(msg) from exc
        return tuple(tool.name for tool in result.tools)

    async def list_tool_details(self) -> tuple[ToolDetails, ...]:
        """Return tool name + description + input schema for every tool.

        FEAT-014's router needs the descriptions and JSON Schemas to
        build an :func:`intents.build_intents_schema` payload. The names
        accessor (:meth:`list_tools`) stays for callers that only need
        the connectivity probe.
        """
        try:
            result = await self._session.list_tools()
        except Exception as exc:
            _LOGGER.debug("list_tool_details raised %s: %r", type(exc).__name__, exc)
            msg = "list_tool_details failed; the MCP subprocess may have exited"
            raise MCPUnavailableError(msg) from exc
        return tuple(
            ToolDetails(
                name=tool.name,
                description=tool.description or "",
                input_schema=dict(tool.inputSchema or {}),
            )
            for tool in result.tools
        )

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
            _LOGGER.debug("call_tool(%r) raised %s: %r", name, type(exc).__name__, exc)
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
            env=_subprocess_env(os.environ),
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
    """Concatenate every text fragment in *content* into a single string.

    Non-text content items (e.g. ``ImageContent``, ``EmbeddedResource``)
    are dropped with a DEBUG log so FEAT-014 authors discovering a
    missing tool response can see what was discarded.
    """
    parts: list[str] = []
    for item in content:
        text = getattr(item, "text", None)
        if text is None:
            _LOGGER.debug("dropping non-text content item: %r", type(item).__name__)
            continue
        parts.append(str(text))
    return "\n".join(parts)
