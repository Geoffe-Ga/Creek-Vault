"""Bootstrap + registration tests for the creek-tools MCP server (FEAT-010)."""

from __future__ import annotations

import asyncio
import os
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
    "creek.save",
    "creek.ingest",
    "creek.redact.scan",
    "creek.classify",
    "creek.link",
    "creek.report",
    "creek.skills.refresh",
    "creek.compile",
    "creek.purge.fragment",
    "creek.purge.source",
    "creek.purge.classifications",
    "creek.purge.daterange",
    "creek.purge.vault",
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


def test_build_server_registers_all_tools(vault: Path) -> None:
    """All FEAT-010 read + FEAT-011 write tools surface via ``list_tools``."""
    server = build_server(
        vault_path=vault,
        draft_llm_factory=lambda: lambda prompt: "ignored",
    )
    tools = asyncio.run(server.list_tools())
    names = {tool.name for tool in tools}
    assert names == EXPECTED_TOOLS


def test_call_tool_save_through_mcp(vault: Path) -> None:
    """End-to-end: ``call_tool("creek.save")`` writes the note and returns path."""
    for relparts in (
        ("02-Threads", "Active"),
        ("00-Creek-Meta", "audit"),
    ):
        (vault.joinpath(*relparts)).mkdir(parents=True, exist_ok=True)
    server = build_server(
        vault_path=vault,
        draft_llm_factory=lambda: lambda prompt: "ignored",
    )
    result = asyncio.run(
        server.call_tool(
            "creek.save",
            {
                "target": "thread",
                "body": "Note worth keeping.",
                "title": "Saved thread",
                "tier": "open",
                "privacy_tier_ceiling": "open",
            },
        ),
    )
    structured = _structured(result)
    assert structured["status"] == "ok"
    assert structured["tool"] == "creek.save"


def test_every_tool_requires_privacy_tier_ceiling_parameter(vault: Path) -> None:
    """The FEAT-010 acceptance criterion: ceiling is in every tool's schema.

    FEAT-012 carve-out: ``creek.purge.*`` tools don't read vault
    content — they take an elevated ``auth_token`` instead — so the
    tier-ceiling invariant doesn't apply to them.
    """
    server = build_server(
        vault_path=vault,
        draft_llm_factory=lambda: lambda prompt: "ignored",
    )
    tools = asyncio.run(server.list_tools())
    for tool in tools:
        if tool.name.startswith("creek.purge."):
            continue
        schema = tool.inputSchema
        assert "privacy_tier_ceiling" in schema["properties"], (
            f"{tool.name} missing privacy_tier_ceiling in its input schema"
        )


def test_purge_tools_require_auth_token_parameter(vault: Path) -> None:
    """FEAT-012: every ``creek.purge.*`` tool exposes an ``auth_token`` slot."""
    server = build_server(
        vault_path=vault,
        draft_llm_factory=lambda: lambda prompt: "ignored",
    )
    tools = asyncio.run(server.list_tools())
    purge_tools = [t for t in tools if t.name.startswith("creek.purge.")]
    assert len(purge_tools) == 5
    for tool in purge_tools:
        schema = tool.inputSchema
        assert "auth_token" in schema["properties"], (
            f"{tool.name} missing auth_token in its input schema"
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
    server_module.main([])
    assert runs == [("stdio",)]


def test_main_config_flag_sets_env_var_before_build_server(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """``--config <path>`` sets ``CREEK_CONFIG`` before tools register (INC-008)."""
    from creek.config import CONFIG_PATH_ENV_VAR
    from creek_mcp import server as server_module

    config_file = tmp_path / "vault-config.yaml"
    config_file.write_text("timezone: UTC\n")
    monkeypatch.delenv(CONFIG_PATH_ENV_VAR, raising=False)

    captured_env: dict[str, str | None] = {}

    class _StubServer:
        def run(self, transport: str) -> None:
            del transport

    def _capture_build() -> _StubServer:
        captured_env[CONFIG_PATH_ENV_VAR] = os.environ.get(CONFIG_PATH_ENV_VAR)
        return _StubServer()

    monkeypatch.setattr(server_module, "build_server", _capture_build)
    server_module.main(["--config", str(config_file)])

    assert captured_env[CONFIG_PATH_ENV_VAR] == str(config_file.resolve())


def test_main_config_flag_missing_file_errors(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``--config`` pointing at a missing file exits nonzero with a clear message."""
    from creek_mcp import server as server_module

    missing = tmp_path / "no-such-config.yaml"

    # build_server must not be called when --config is invalid.
    def _explode() -> object:  # pragma: no cover - asserts non-invocation
        msg = "build_server should not run when --config is missing"
        raise AssertionError(msg)

    monkeypatch.setattr(server_module, "build_server", _explode)

    with pytest.raises(SystemExit) as exc_info:
        server_module.main(["--config", str(missing)])

    assert exc_info.value.code != 0
    err = capsys.readouterr().err
    assert "file not found" in err.lower()


def test_main_without_config_flag_leaves_env_var_untouched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Omitting ``--config`` does not modify ``CREEK_CONFIG``."""
    from creek.config import CONFIG_PATH_ENV_VAR
    from creek_mcp import server as server_module

    monkeypatch.delenv(CONFIG_PATH_ENV_VAR, raising=False)

    class _StubServer:
        def run(self, transport: str) -> None:
            del transport

    monkeypatch.setattr(server_module, "build_server", lambda: _StubServer())
    server_module.main([])

    assert CONFIG_PATH_ENV_VAR not in os.environ
