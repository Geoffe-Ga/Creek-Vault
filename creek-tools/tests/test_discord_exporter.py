"""Opt-in user-token DM exporter connector (#686).

Tested with a fake exporter — no real network, no real token. Proves: off by
default, caveat on enable, token from env (never logged), incremental
``--after`` + your-DMs-only + read-only flags, staged JSON ingested, cursor
advanced, and a re-run is idempotent.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

import pytest

from creek.config import DiscordSourceConfig
from creek.ingest.discord_dispatch import (
    ExporterConnector,
    ExporterDisabledError,
    SubprocessExporterRunner,
    TokenMissingError,
)

if TYPE_CHECKING:
    from pathlib import Path

_CURSOR = "2026-06-25T00:00:00+00:00"


def _write_data_package(out_dir: Path) -> None:
    """Write a minimal one-DM Data Package the DiscordIngestor can read."""
    channel = out_dir / "messages" / "chan1"
    channel.mkdir(parents=True, exist_ok=True)
    (channel / "channel.json").write_text(
        json.dumps({"id": "chan1", "name": "Direct Message with Ada"}),
        encoding="utf-8",
    )
    (channel / "messages.json").write_text(
        json.dumps(
            [
                {
                    "id": "m1",
                    "timestamp": "2026-06-26T10:00:00+00:00",
                    "content": "a private reflection worth keeping",
                    "author": {"name": "me"},
                },
            ],
        ),
        encoding="utf-8",
    )


class FakeExporter:
    """Records its invocation and stages a Data Package (no network)."""

    def __init__(self) -> None:
        """Initialise capture fields."""
        self.argv: list[str] | None = None
        self.token: str | None = None
        self.out_dir: Path | None = None
        self.scope: str | None = None
        self.read_only: bool = False

    def run(self, *, argv: list[str], token: str, out_dir: Path) -> None:
        """Capture the call and stage the sample DM export."""
        self.argv = argv
        self.token = token
        self.out_dir = out_dir
        self.scope = argv[argv.index("--scope") + 1] if "--scope" in argv else None
        self.read_only = "--read-only" in argv
        _write_data_package(out_dir)


def _make_vault(tmp_path: Path) -> Path:
    """Scaffold a vault the connector can ingest into."""
    vault = tmp_path / "vault"
    for d in ("00-Creek-Meta/Processing-Log", "01-Fragments/Messages"):
        (vault / d).mkdir(parents=True, exist_ok=True)
    return vault


def _fragments(vault: Path) -> list[Path]:
    """Every fragment written under 01-Fragments."""
    return sorted((vault / "01-Fragments").rglob("*.md"))


class TestExporterConnector:
    """The connector exports incrementally, ingests, and advances the cursor."""

    def test_incremental_readonly_dms_only(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Token from env (never logged); scoped+read-only flags; staged+ingested."""
        monkeypatch.setenv("DISCORD_USER_TOKEN", "secret-xyz")
        fake = FakeExporter()
        cfg = DiscordSourceConfig(exporter={"enabled": True})
        vault = _make_vault(tmp_path)
        connector = ExporterConnector(cfg, runner=fake, vault=vault)

        with caplog.at_level(logging.INFO):
            connector.run(after_cursor=_CURSOR)

        assert fake.token == "secret-xyz"
        assert "secret-xyz" not in caplog.text  # token never logged
        assert "--after" in fake.argv
        assert _CURSOR in fake.argv
        assert fake.scope == "dms-only"
        assert fake.read_only is True
        assert len(_fragments(vault)) > 0  # staged JSON was ingested
        cursor = connector.load_cursor()
        assert cursor is not None
        assert cursor > _CURSOR  # advanced past the input window

    def test_refuses_when_disabled(self, tmp_path: Path) -> None:
        """A disabled exporter raises before any work."""
        connector = ExporterConnector(
            DiscordSourceConfig(), runner=FakeExporter(), vault=tmp_path
        )
        with pytest.raises(ExporterDisabledError):
            connector.run()

    def test_requires_token(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An enabled exporter with no token env var raises TokenMissingError."""
        monkeypatch.delenv("DISCORD_USER_TOKEN", raising=False)
        cfg = DiscordSourceConfig(exporter={"enabled": True})
        connector = ExporterConnector(
            cfg, runner=FakeExporter(), vault=_make_vault(tmp_path)
        )
        with pytest.raises(TokenMissingError):
            connector.run()

    def test_prints_tos_caveat_on_run(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Running the (enabled) exporter prints the ToS caveat."""
        monkeypatch.setenv("DISCORD_USER_TOKEN", "x")
        cfg = DiscordSourceConfig(exporter={"enabled": True})
        ExporterConnector(cfg, runner=FakeExporter(), vault=_make_vault(tmp_path)).run(
            after_cursor=_CURSOR
        )
        out = capsys.readouterr().out
        assert "Discord Terms of Service" in out
        assert "read-only" in out

    def test_rerun_is_idempotent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A second run (persisted cursor, same messages) writes no duplicate."""
        monkeypatch.setenv("DISCORD_USER_TOKEN", "x")
        cfg = DiscordSourceConfig(exporter={"enabled": True})
        vault = _make_vault(tmp_path)

        ExporterConnector(cfg, runner=FakeExporter(), vault=vault).run(
            after_cursor=_CURSOR
        )
        before = len(_fragments(vault))
        assert before > 0

        # New instance resumes from the persisted cursor; same export, same ids.
        ExporterConnector(cfg, runner=FakeExporter(), vault=vault).run()
        assert len(_fragments(vault)) == before  # idempotent (stable ids)


class TestSubprocessExporterRunner:
    """The real runner passes the token via env, never on the command line."""

    def test_token_via_env_not_argv(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The token reaches the child env; the argv carries no secret."""
        captured: dict[str, object] = {}

        def _fake_run(cmd: list[str], **kwargs: object) -> None:
            captured["cmd"] = cmd
            captured["env"] = kwargs.get("env")

        monkeypatch.setattr("creek.ingest.discord_dispatch.subprocess.run", _fake_run)
        runner = SubprocessExporterRunner("/usr/local/bin/exporter")
        runner.run(
            argv=["--after", _CURSOR, "--read-only"],
            token="secret-zzz",
            out_dir=tmp_path,
        )

        cmd = captured["cmd"]
        assert cmd[0] == "/usr/local/bin/exporter"
        assert "--read-only" in cmd
        assert "secret-zzz" not in cmd  # token never on the command line
        env = captured["env"]
        assert isinstance(env, dict)
        assert env["DISCORD_USER_TOKEN"] == "secret-zzz"
