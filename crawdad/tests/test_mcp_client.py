"""Tests for ``crawdad.mcp_client``."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest
from mcp.client.stdio import get_default_environment

import crawdad.mcp_client as mcp_client
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
    broken: Any = _BrokenSession()
    session = MCPSession(broken)

    with pytest.raises(MCPUnavailableError, match="list_tools"):
        await session.list_tools()


async def test_session_call_tool_translates_underlying_failure() -> None:
    """The adapter maps SDK errors to :class:`MCPUnavailableError`."""
    broken: Any = _BrokenSession()
    session = MCPSession(broken)

    with pytest.raises(MCPUnavailableError, match="call_tool"):
        await session.call_tool("anything", {})


async def test_list_tool_details_returns_name_description_and_schema() -> None:
    """``list_tool_details`` powers FEAT-014's intents-schema generation."""
    async with MCPClient(_fixture_command()).connect() as session:
        details = await session.list_tool_details()

    names = {tool.name for tool in details}
    assert {"echo", "ping"}.issubset(names)
    echo = next(tool for tool in details if tool.name == "echo")
    assert echo.input_schema["type"] == "object"
    # FastMCP wraps the function signature; ``message`` is the kwarg.
    assert "message" in echo.input_schema.get("properties", {})


async def test_list_tool_details_translates_underlying_failure() -> None:
    """Failures during the detailed listing map to MCPUnavailableError."""
    from crawdad.mcp_client import MCPSession

    broken: Any = _BrokenSession()
    session = MCPSession(broken)

    with pytest.raises(MCPUnavailableError, match="list_tool_details"):
        await session.list_tool_details()


# --- Issue #549: forward credentials the stdio SDK would otherwise scrub ------


def test_subprocess_env_forwards_anthropic_api_key() -> None:
    """The API key the stdio SDK drops is re-added from the source env."""
    env = mcp_client._subprocess_env({"ANTHROPIC_API_KEY": "sk-test-123"})

    assert env["ANTHROPIC_API_KEY"] == "sk-test-123"


def test_subprocess_env_keeps_sdk_safe_defaults() -> None:
    """Forwarding augments, never replaces, the SDK default-environment allowlist."""
    env = mcp_client._subprocess_env({"ANTHROPIC_API_KEY": "sk-test-123"})

    for key, value in get_default_environment().items():
        assert env[key] == value


def test_subprocess_env_omits_absent_credentials() -> None:
    """A source env without the credentials yields no key — no blank entry."""
    env = mcp_client._subprocess_env({})

    assert "ANTHROPIC_API_KEY" not in env
    assert "CREEK_ANTHROPIC_CONSENT" not in env


def test_subprocess_env_treats_blank_value_as_unset() -> None:
    """An empty-string credential is not forwarded (would disable the provider)."""
    env = mcp_client._subprocess_env({"ANTHROPIC_API_KEY": ""})

    assert "ANTHROPIC_API_KEY" not in env


def test_subprocess_env_forwards_consent_only_when_present() -> None:
    """Cloud-egress consent is opt-in: forwarded iff set in the source env."""
    without = mcp_client._subprocess_env({"ANTHROPIC_API_KEY": "sk"})
    assert "CREEK_ANTHROPIC_CONSENT" not in without

    with_consent = mcp_client._subprocess_env(
        {"ANTHROPIC_API_KEY": "sk", "CREEK_ANTHROPIC_CONSENT": "1"},
    )
    assert with_consent["CREEK_ANTHROPIC_CONSENT"] == "1"


def test_subprocess_env_ignores_unlisted_vars() -> None:
    """Only the documented allowlist is forwarded; other env vars never leak."""
    env = mcp_client._subprocess_env(
        {"SECRET_TOKEN": "nope", "ANTHROPIC_API_KEY": "sk"},
    )

    assert "SECRET_TOKEN" not in env


async def test_connect_passes_forwarded_env_to_subprocess(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``connect`` hands StdioServerParameters an env carrying the API key.

    Regression guard for issue #549: without an explicit ``env=``, the stdio
    SDK scrubs ANTHROPIC_API_KEY and in-MCP cloud tools silently degrade.
    """
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-connect-test")
    captured: dict[str, Any] = {}
    real_params = mcp_client.StdioServerParameters

    def _spy(**kwargs: Any) -> Any:
        captured.update(kwargs)
        return real_params(**kwargs)

    monkeypatch.setattr(mcp_client, "StdioServerParameters", _spy)

    async with MCPClient(_fixture_command()).connect() as session:
        await session.list_tools()

    assert captured["env"]["ANTHROPIC_API_KEY"] == "sk-connect-test"
