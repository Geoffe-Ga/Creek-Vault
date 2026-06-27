"""Discord ingest-mode dispatch skeleton (#685).

Each mode resolves to a stub handler that echoes a plan; the exporter is OFF by
default and prints a Terms-of-Service caveat on enable. No handler performs
network I/O.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from typer.testing import CliRunner

from creek.cli import app
from creek.config import DiscordSourceConfig
from creek.ingest.discord_dispatch import (
    DiscordMode,
    ExporterDisabledError,
    ModeDisabledError,
    resolve_discord_handler,
)

if TYPE_CHECKING:
    from pathlib import Path

runner = CliRunner()


# ---- Config defaults ----------------------------------------------------


class TestDiscordConfigDefaults:
    """The exporter is off by default; the Data Package fallback is on."""

    def test_exporter_off_by_default(self) -> None:
        """Decision #3: the user-token exporter defaults OFF."""
        cfg = DiscordSourceConfig()
        assert cfg.exporter.enabled is False

    def test_data_package_on_by_default(self) -> None:
        """The clean Data Package fallback is available by default."""
        assert DiscordSourceConfig().data_package.enabled is True

    def test_bot_capture_off_by_default(self) -> None:
        """Bot capture is opt-in."""
        assert DiscordSourceConfig().bot_capture.enabled is False


# ---- resolve_discord_handler -------------------------------------------


class TestResolveHandler:
    """Mode resolution gates on the toggle; no handler does network I/O."""

    def test_exporter_disabled_refuses(self) -> None:
        """Resolving the disabled exporter raises (no pull attempted)."""
        with pytest.raises(ExporterDisabledError):
            resolve_discord_handler(DiscordMode.EXPORTER, DiscordSourceConfig())

    def test_disabled_mode_raises_mode_disabled(self) -> None:
        """A disabled non-exporter mode raises ModeDisabledError."""
        cfg = DiscordSourceConfig(bot_capture={"enabled": False})
        with pytest.raises(ModeDisabledError):
            resolve_discord_handler(DiscordMode.BOT_CAPTURE, cfg)

    def test_enabled_exporter_announces_tos_caveat(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """An enabled exporter announces the full ToS caveat."""
        cfg = DiscordSourceConfig(exporter={"enabled": True})
        handler = resolve_discord_handler(DiscordMode.EXPORTER, cfg)
        handler.announce()
        out = capsys.readouterr().out
        assert "Discord Terms of Service" in out
        assert "account suspension" in out
        assert "your own DMs" in out
        assert "read-only" in out

    def test_each_mode_resolves_and_plans_without_network(self) -> None:
        """data_package + bot_capture resolve and echo a plan (no I/O)."""
        cfg = DiscordSourceConfig(
            data_package={"enabled": True}, bot_capture={"enabled": True}
        )
        for mode in (DiscordMode.DATA_PACKAGE, DiscordMode.BOT_CAPTURE):
            handler = resolve_discord_handler(mode, cfg)
            plan = handler.plan()
            assert plan  # echoes a plan
            assert any("no network performed" in line for line in plan)


# ---- CLI: creek discord --mode -----------------------------------------


class TestDiscordCommand:
    """The CLI resolves the mode and echoes the plan; errors exit non-zero."""

    def test_bot_capture_dispatch(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """bot_capture (enabled) echoes its plan and the no-network note."""
        from creek.config import CreekConfig

        cfg = CreekConfig(discord=DiscordSourceConfig(bot_capture={"enabled": True}))
        monkeypatch.setattr("creek.cli._load_config_for_vault", lambda _v: cfg)
        result = runner.invoke(
            app, ["discord", "--mode", "bot_capture", "--vault", str(tmp_path)]
        )
        assert result.exit_code == 0, result.output
        assert "mode=bot_capture" in result.output
        assert "no network performed" in result.output

    def test_exporter_disabled_exits_nonzero(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The default-off exporter mode exits non-zero (not run)."""
        from creek.config import CreekConfig

        monkeypatch.setattr(
            "creek.cli._load_config_for_vault", lambda _v: CreekConfig()
        )
        result = runner.invoke(
            app, ["discord", "--mode", "exporter", "--vault", str(tmp_path)]
        )
        assert result.exit_code != 0
        assert "exporter" in result.output.lower()

    def test_unknown_mode_exits_nonzero(self, tmp_path: Path) -> None:
        """An unknown mode is rejected before any config use."""
        result = runner.invoke(
            app, ["discord", "--mode", "telepathy", "--vault", str(tmp_path)]
        )
        assert result.exit_code != 0
        assert "mode" in result.output.lower()


class TestDiscordDataPackageCli:
    """`creek discord --mode data_package --package` routes to the helper (#688)."""

    def test_package_runs_helper(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """data_package + --package (no --dry-run) calls the real helper."""
        from creek.config import CreekConfig

        called: list[tuple[Path, Path]] = []
        monkeypatch.setattr(
            "creek.cli._run_discord_data_package",
            lambda vault, package: called.append((vault, package)),
        )
        monkeypatch.setattr(
            "creek.cli._load_config_for_vault", lambda _v: CreekConfig()
        )
        pkg = tmp_path / "discord-export"
        pkg.mkdir()

        result = runner.invoke(
            app,
            [
                "discord",
                "--mode",
                "data_package",
                "--package",
                str(pkg),
                "--vault",
                str(tmp_path),
            ],
        )
        assert result.exit_code == 0, result.output
        assert called == [(tmp_path, pkg)]

    def test_dry_run_echoes_not_runs_helper(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """--dry-run echoes the plan and does NOT invoke the helper."""
        from creek.config import CreekConfig

        called: list[object] = []
        monkeypatch.setattr(
            "creek.cli._run_discord_data_package",
            lambda vault, package: called.append((vault, package)),
        )
        monkeypatch.setattr(
            "creek.cli._load_config_for_vault", lambda _v: CreekConfig()
        )
        pkg = tmp_path / "discord-export"
        pkg.mkdir()

        result = runner.invoke(
            app,
            [
                "discord",
                "--mode",
                "data_package",
                "--package",
                str(pkg),
                "--dry-run",
                "--vault",
                str(tmp_path),
            ],
        )
        assert result.exit_code == 0, result.output
        assert called == []  # dry-run -> stub echo, helper not called
        assert "data_package" in result.output

    def test_missing_package_exits_nonzero(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A pointed-at package that does not exist exits non-zero."""
        from creek.config import CreekConfig

        monkeypatch.setattr(
            "creek.cli._load_config_for_vault", lambda _v: CreekConfig()
        )
        result = runner.invoke(
            app,
            [
                "discord",
                "--mode",
                "data_package",
                "--package",
                str(tmp_path / "absent"),
                "--vault",
                str(tmp_path),
            ],
        )
        assert result.exit_code == 1
        assert "does not exist" in result.output
