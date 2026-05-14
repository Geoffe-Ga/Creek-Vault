"""Tests for ``crawdad.cli`` — the ``crawdad run`` entry point."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from crawdad import cli
from crawdad.config import CrawDadConfig


@pytest.fixture
def patched_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> CrawDadConfig:
    """Install a minimal config and stub out the actual Discord connection."""
    config_path = tmp_path / "crawdad.yaml"
    vault = tmp_path / "vault"
    vault.mkdir()
    config_path.write_text(
        "vault_path: " + str(vault) + "\n"
        "allowed_user_ids: [1]\n"
        "allowed_channel_ids: [2]\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "t")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")

    config = CrawDadConfig(
        discord_bot_token="t",
        anthropic_api_key="k",
        vault_path=vault,
        allowed_user_ids=[1],
        allowed_channel_ids=[2],
    )
    monkeypatch.setattr(cli, "load_config", lambda _path=None: config)
    return config


def test_main_invokes_runtime_with_config(
    patched_config: CrawDadConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``crawdad run`` resolves the config and delegates to the runtime."""
    captured: dict[str, Any] = {}

    def _fake_run(config: CrawDadConfig) -> None:
        captured["config"] = config

    monkeypatch.setattr(cli, "run_bot", _fake_run)

    cli.main(["run"])

    assert captured["config"] is patched_config


def test_main_run_with_config_path(
    patched_config: CrawDadConfig, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``--config <path>`` forwards the path into ``load_config``."""
    seen: dict[str, Any] = {}

    def _fake_load(path: Path | None = None) -> CrawDadConfig:
        seen["path"] = path
        return patched_config

    monkeypatch.setattr(cli, "load_config", _fake_load)
    monkeypatch.setattr(cli, "run_bot", lambda _c: None)
    yaml_path = tmp_path / "alt.yaml"
    yaml_path.write_text("vault_path: /tmp\n", encoding="utf-8")

    cli.main(["run", "--config", str(yaml_path)])

    assert seen["path"] == yaml_path


def test_main_requires_subcommand(capsys: pytest.CaptureFixture[str]) -> None:
    """Bare ``crawdad`` (no subcommand) exits with usage info."""
    with pytest.raises(SystemExit):
        cli.main([])


def test_run_bot_wires_state_and_starts_client(
    patched_config: CrawDadConfig,
    monkeypatch: pytest.MonkeyPatch,
    vault_with_state: Path,
) -> None:
    """``run_bot`` loads state, probes MCP, and hands the token to discord."""
    config = patched_config.model_copy(update={"vault_path": vault_with_state})
    captured: dict[str, Any] = {}

    class _FakeClient:
        def __init__(self, **kwargs: Any) -> None:
            captured["init_kwargs"] = kwargs

        def run(self, token: str) -> None:
            captured["token"] = token

    async def _ok_probe(_c: CrawDadConfig) -> None:
        captured["probed"] = True

    monkeypatch.setattr(cli, "CrawDadClient", _FakeClient)
    monkeypatch.setattr(cli, "_probe_mcp", _ok_probe)

    cli.run_bot(config)

    assert captured["token"] == config.discord_bot_token
    assert captured["probed"] is True
    assert captured["init_kwargs"]["session_state"] is not None


def test_run_bot_swallows_missing_state(
    patched_config: CrawDadConfig,
    monkeypatch: pytest.MonkeyPatch,
    empty_vault: Path,
) -> None:
    """Missing ``latest.md`` does not crash startup — session_state goes None."""
    config = patched_config.model_copy(update={"vault_path": empty_vault})
    captured: dict[str, Any] = {}

    class _FakeClient:
        def __init__(self, **kwargs: Any) -> None:
            captured["init_kwargs"] = kwargs

        def run(self, _token: str) -> None:
            captured["ran"] = True

    async def _ok_probe(_c: CrawDadConfig) -> None:
        return None

    monkeypatch.setattr(cli, "CrawDadClient", _FakeClient)
    monkeypatch.setattr(cli, "_probe_mcp", _ok_probe)

    cli.run_bot(config)

    assert captured["init_kwargs"]["session_state"] is None
    assert captured["ran"] is True


async def test_probe_mcp_logs_tools_on_success(
    patched_config: CrawDadConfig,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A working MCP subprocess produces an INFO log listing tool names."""
    from contextlib import asynccontextmanager

    class _Session:
        async def list_tools(self) -> tuple[str, ...]:
            return ("echo",)

    class _Client:
        def __init__(self, _command: tuple[str, ...]) -> None:
            self._command = _command

        @asynccontextmanager
        async def connect(self) -> Any:
            yield _Session()

    monkeypatch.setattr(cli, "MCPClient", _Client)
    caplog.set_level("INFO", logger="crawdad.cli")

    await cli._probe_mcp(patched_config)

    assert any("echo" in record.message for record in caplog.records)


async def test_probe_mcp_logs_warning_on_failure(
    patched_config: CrawDadConfig,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A broken MCP probe logs a warning but does NOT raise — bot keeps running."""
    from contextlib import asynccontextmanager

    class _Client:
        def __init__(self, _command: tuple[str, ...]) -> None:
            self._command = _command

        @asynccontextmanager
        async def connect(self) -> Any:
            from crawdad.mcp_client import MCPUnavailableError

            msg = "boom"
            raise MCPUnavailableError(msg)
            yield  # pragma: no cover - unreachable

    monkeypatch.setattr(cli, "MCPClient", _Client)
    caplog.set_level("WARNING", logger="crawdad.cli")

    await cli._probe_mcp(patched_config)

    assert any("MCP probe failed" in record.message for record in caplog.records)
