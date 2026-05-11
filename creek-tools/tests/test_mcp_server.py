"""Bootstrap + registration tests for the creek-tools MCP server (FEAT-010)."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest

from creek_mcp.server import SERVER_NAME, build_server

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


EXPECTED_TOOLS = {"creek.state.read", "creek.state.render"}


@pytest.fixture
def vault(tmp_path: Path) -> Iterator[Path]:
    """Yield a freshly seeded vault for the server tests."""
    for sub in (
        "00-Creek-Meta/State",
        "00-Creek-Meta/Processing-Log",
        "00-Creek-Meta/audit",
        "01-Fragments/Notes",
        "02-Threads/Active",
        "03-Eddies",
    ):
        (tmp_path / sub).mkdir(parents=True, exist_ok=True)
    yield tmp_path


def _structured(result: object) -> dict[str, object]:
    """Pull the structured-content dict out of a FastMCP ``call_tool`` result."""
    return result[1] if isinstance(result, tuple) else result  # type: ignore[return-value, index]


def test_build_server_returns_fastmcp_instance(vault: Path) -> None:
    """The bootstrap returns a configured :class:`FastMCP` instance."""
    server = build_server(vault_path=vault)
    assert server.name == SERVER_NAME


def test_build_server_registers_part_one_tools(vault: Path) -> None:
    """The state read/render tools surface via ``list_tools``.

    FEAT-010 part 2 adds ``creek.lint``, ``creek.mine``, and
    ``creek.draft`` — those land in the follow-up PR. This test pins
    the part-1 surface so the registration regression is caught early.
    """
    server = build_server(vault_path=vault)
    tools = asyncio.run(server.list_tools())
    names = {tool.name for tool in tools}
    assert names == EXPECTED_TOOLS


def test_every_tool_requires_privacy_tier_ceiling_parameter(vault: Path) -> None:
    """The FEAT-010 acceptance criterion: ceiling is in every tool's schema."""
    server = build_server(vault_path=vault)
    tools = asyncio.run(server.list_tools())
    for tool in tools:
        schema = tool.inputSchema
        assert "privacy_tier_ceiling" in schema["properties"], (
            f"{tool.name} missing privacy_tier_ceiling in its input schema"
        )


def test_call_tool_state_read_through_mcp(vault: Path) -> None:
    """End-to-end: ``call_tool("creek.state.read")`` returns the report bytes."""
    (vault / "00-Creek-Meta" / "State" / "latest.md").write_text(
        "# Audit\n\nhello\n",
        encoding="utf-8",
    )
    server = build_server(vault_path=vault)
    result = asyncio.run(
        server.call_tool("creek.state.read", {"privacy_tier_ceiling": "open"}),
    )
    structured = _structured(result)
    assert structured["status"] == "ok"
    assert structured["tool"] == "creek.state.read"
    assert "Audit" in structured["content"]  # type: ignore[operator]


def test_call_tool_state_render_through_mcp(vault: Path) -> None:
    """The render path is reachable via ``call_tool``."""
    server = build_server(vault_path=vault)
    result = asyncio.run(
        server.call_tool(
            "creek.state.render",
            {"privacy_tier_ceiling": "open"},
        ),
    )
    structured = _structured(result)
    assert structured["status"] == "ok"


def test_build_server_falls_back_to_load_config(
    vault: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without an explicit ``vault_path``, the bootstrap reads ``load_config``."""

    class _StubConfig:
        vault_path = vault

    monkeypatch.setattr("creek_mcp.server.load_config", lambda: _StubConfig())
    server = build_server()
    assert server.name == SERVER_NAME


def test_main_invokes_server_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """``main()`` calls ``FastMCP.run(transport='stdio')``."""
    from creek_mcp import server as server_module

    runs: list[tuple[object, ...]] = []

    class _StubServer:
        def run(self, transport: str) -> None:
            runs.append((transport,))

    monkeypatch.setattr(server_module, "build_server", lambda: _StubServer())
    server_module.main()
    assert runs == [("stdio",)]
