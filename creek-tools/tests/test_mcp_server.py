"""Bootstrap + registration tests for the creek-tools MCP server (FEAT-010)."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest

from creek_mcp.server import SERVER_NAME, build_server

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


EXPECTED_TOOLS = {
    "creek.state.read",
    "creek.state.render",
    "creek.lint",
    "creek.mine",
    "creek.draft",
}


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
        "creek-skills",
    ):
        (tmp_path / sub).mkdir(parents=True, exist_ok=True)
    yield tmp_path


def _structured(result: object) -> dict[str, object]:
    """Pull the structured-content dict out of a FastMCP ``call_tool`` result."""
    return result[1] if isinstance(result, tuple) else result  # type: ignore[return-value, index]


def test_build_server_returns_fastmcp_instance(vault: Path) -> None:
    """The bootstrap returns a configured :class:`FastMCP` instance."""
    server = build_server(
        vault_path=vault,
        draft_llm_factory=lambda: lambda prompt: "ignored",
    )
    assert server.name == SERVER_NAME


def test_build_server_registers_five_read_tools(vault: Path) -> None:
    """All five FEAT-010 read tools surface via ``list_tools``."""
    server = build_server(
        vault_path=vault,
        draft_llm_factory=lambda: lambda prompt: "ignored",
    )
    tools = asyncio.run(server.list_tools())
    names = {tool.name for tool in tools}
    assert names == EXPECTED_TOOLS


def test_every_tool_requires_privacy_tier_ceiling_parameter(vault: Path) -> None:
    """The FEAT-010 acceptance criterion: ceiling is in every tool's schema."""
    server = build_server(
        vault_path=vault,
        draft_llm_factory=lambda: lambda prompt: "ignored",
    )
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
    server = build_server(
        vault_path=vault,
        draft_llm_factory=lambda: lambda prompt: "ignored",
    )
    result = asyncio.run(
        server.call_tool("creek.state.read", {"privacy_tier_ceiling": "open"}),
    )
    structured = _structured(result)
    assert structured["status"] == "ok"
    assert structured["tool"] == "creek.state.read"
    assert "Audit" in structured["content"]  # type: ignore[operator]


def test_call_tool_state_render_through_mcp(vault: Path) -> None:
    """The render path is reachable via ``call_tool``."""
    server = build_server(
        vault_path=vault,
        draft_llm_factory=lambda: lambda prompt: "ignored",
    )
    result = asyncio.run(
        server.call_tool(
            "creek.state.render",
            {"privacy_tier_ceiling": "open"},
        ),
    )
    structured = _structured(result)
    assert structured["status"] == "ok"


def test_call_tool_lint_through_mcp(vault: Path) -> None:
    """The lint path is reachable via ``call_tool`` and returns ``checks``."""
    server = build_server(
        vault_path=vault,
        draft_llm_factory=lambda: lambda prompt: "ignored",
    )
    result = asyncio.run(
        server.call_tool("creek.lint", {"privacy_tier_ceiling": "open"}),
    )
    structured = _structured(result)
    assert structured["status"] == "ok"
    assert "checks" in structured


def test_call_tool_mine_through_mcp(vault: Path) -> None:
    """The mine path is reachable via ``call_tool``."""
    server = build_server(
        vault_path=vault,
        draft_llm_factory=lambda: lambda prompt: "ignored",
    )
    result = asyncio.run(
        server.call_tool(
            "creek.mine",
            {"privacy_tier_ceiling": "open", "phase": "rising", "limit": 3},
        ),
    )
    structured = _structured(result)
    assert structured["status"] == "ok"


def test_call_tool_draft_through_mcp(vault: Path) -> None:
    """The draft path is reachable via ``call_tool``."""
    server = build_server(
        vault_path=vault,
        draft_llm_factory=lambda: lambda prompt: "ignored",
    )
    result = asyncio.run(
        server.call_tool(
            "creek.draft",
            {"privacy_tier_ceiling": "open", "phase": "rising"},
        ),
    )
    structured = _structured(result)
    # Empty vault → no seeds; tool returns structured ``empty``.
    assert structured["status"] == "empty"


def test_build_server_falls_back_to_load_config(
    vault: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without an explicit ``vault_path``, the bootstrap reads ``load_config``."""

    class _StubConfig:
        vault_path = vault

    monkeypatch.setattr("creek_mcp.server.load_config", lambda: _StubConfig())
    server = build_server(draft_llm_factory=lambda: lambda prompt: "x")
    assert server.name == SERVER_NAME


def test_build_draft_llm_raises_when_classifier_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The production factory bubbles a clear error when no LLM is reachable."""
    from creek_mcp import server as server_module

    class _UnavailableClassifier:
        available = False

        def __init__(self, _config: object) -> None:
            pass

        def invoke_prompt(self, prompt: str) -> str:  # pragma: no cover
            return ""

    monkeypatch.setattr(
        "creek.classify.llm.LLMClassifier",
        _UnavailableClassifier,
    )
    with pytest.raises(RuntimeError, match="LLM provider unavailable"):
        server_module._build_draft_llm()


def test_build_draft_llm_returns_invoke_prompt_when_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the classifier reports ``available``, the factory returns the callable."""
    from creek_mcp import server as server_module

    class _AvailableClassifier:
        available = True

        def __init__(self, _config: object) -> None:
            pass

        def invoke_prompt(self, prompt: str) -> str:
            return "drafted body"

    monkeypatch.setattr(
        "creek.classify.llm.LLMClassifier",
        _AvailableClassifier,
    )
    llm = server_module._build_draft_llm()
    assert callable(llm)
    assert llm("hi") == "drafted body"


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
