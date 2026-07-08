"""Schedule-emitter tests for ``creek sync --install-schedule`` (#679).

Renders launchd / systemd / cron artifacts parametrised by the config cadence,
and verifies the emitted units are valid and embed the right ``creek sync``
command (Tier A never contains link/index — R6).
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path
from typing import TYPE_CHECKING

from typer.testing import CliRunner

from creek.cli import app
from creek.config import CreekConfig
from creek.sync import (
    render_crontab,
    render_launchd_plists,
    render_systemd_units,
)

if TYPE_CHECKING:
    import pytest

runner = CliRunner()
_VAULT = Path("/Users/me/Vault")


# ---- launchd ------------------------------------------------------------


class TestLaunchd:
    """launchd plists are well-formed and carry the configured cadence."""

    def test_plists_are_valid_xml(self) -> None:
        """Both rendered plists parse as XML."""
        plists = render_launchd_plists(vault=_VAULT)
        ET.fromstring(plists.tier_a)
        ET.fromstring(plists.tier_b)

    def test_tier_a_interval_in_seconds(self) -> None:
        """Tier A StartInterval is the configured minutes, in seconds."""
        plists = render_launchd_plists(vault=_VAULT, tier_a_minutes=30)
        assert "<key>StartInterval</key>" in plists.tier_a
        assert "<integer>1800</integer>" in plists.tier_a  # 30 * 60

    def test_tier_b_nightly_hour(self) -> None:
        """Tier B StartCalendarInterval fires at the configured hour."""
        plists = render_launchd_plists(vault=_VAULT, tier_b_hour=3)
        assert "<key>StartCalendarInterval</key>" in plists.tier_b
        assert "<key>Hour</key>\n      <integer>3</integer>" in plists.tier_b

    def test_command_and_r6(self) -> None:
        """The command targets the vault; Tier A never references link/index."""
        plists = render_launchd_plists(vault=_VAULT)
        assert "<string>creek</string>" in plists.tier_a
        assert f"<string>{_VAULT}</string>" in plists.tier_a
        assert "<string>A</string>" in plists.tier_a
        assert "link" not in plists.tier_a
        assert "index" not in plists.tier_a

    def test_vault_with_spaces_stays_one_string(self) -> None:
        """A spaced vault path is a single <string>, not word-split."""
        spaced = Path("/Users/me/My Notes")
        plists = render_launchd_plists(vault=spaced)
        assert "<string>/Users/me/My Notes</string>" in plists.tier_a
        assert "<string>Notes</string>" not in plists.tier_a  # not split

    def test_xml_special_chars_are_escaped(self) -> None:
        """A vault path with XML metacharacters stays well-formed."""
        plists = render_launchd_plists(vault=Path("/Users/me/A&B"))
        ET.fromstring(plists.tier_a)
        assert "<string>/Users/me/A&amp;B</string>" in plists.tier_a


# ---- systemd ------------------------------------------------------------


class TestSystemd:
    """systemd timers carry the configured OnCalendar; services embed the cmd."""

    def test_timer_uses_configured_cadence(self) -> None:
        """Tier-A every-N-min and Tier-B nightly OnCalendar match config."""
        units = render_systemd_units(vault=_VAULT, tier_a_minutes=30, tier_b_hour=3)
        assert "OnCalendar=*:0/30" in units.tier_a_timer
        assert "OnCalendar=*-*-* 03:00:00" in units.tier_b_timer

    def test_service_embeds_command(self) -> None:
        """Each service ExecStart runs the right tier against the (quoted) vault."""
        units = render_systemd_units(vault=_VAULT)
        assert f'creek sync --tier A --vault "{_VAULT}"' in units.tier_a_service
        assert f'creek sync --tier B --vault "{_VAULT}"' in units.tier_b_service

    def test_tier_a_service_has_no_link_or_index(self) -> None:
        """R6: the Tier-A unit never references link/index."""
        units = render_systemd_units(vault=_VAULT)
        assert "link" not in units.tier_a_service
        assert "index" not in units.tier_a_service

    def test_custom_cadence_is_parametrised(self) -> None:
        """Non-default cadence values flow into the timers."""
        units = render_systemd_units(vault=_VAULT, tier_a_minutes=15, tier_b_hour=5)
        assert "OnCalendar=*:0/15" in units.tier_a_timer
        assert "OnCalendar=*-*-* 05:00:00" in units.tier_b_timer

    def test_vault_with_spaces_is_quoted(self) -> None:
        """A spaced vault path is quoted in ExecStart (one --vault argument)."""
        spaced = Path("/Users/me/My Notes")
        units = render_systemd_units(vault=spaced)
        assert 'ExecStart=creek sync --tier A --vault "/Users/me/My Notes"' in (
            units.tier_a_service
        )


# ---- cron ---------------------------------------------------------------


class TestCron:
    """The crontab lines have the right schedule and command."""

    def test_crontab_schedule_and_command(self) -> None:
        """Tier A every 30 min, Tier B nightly at hour 3, against the vault."""
        cron = render_crontab(vault=_VAULT, tier_a_minutes=30, tier_b_hour=3)
        assert f'*/30 * * * * creek sync --tier A --vault "{_VAULT}"' in cron
        assert f'0 3 * * * creek sync --tier B --vault "{_VAULT}"' in cron

    def test_vault_with_spaces_is_quoted(self) -> None:
        """A spaced vault path is quoted in the cron command."""
        cron = render_crontab(vault=Path("/Users/me/My Notes"))
        assert 'creek sync --tier A --vault "/Users/me/My Notes"' in cron


# ---- CLI: --install-schedule -------------------------------------------


class TestInstallScheduleCli:
    """The command writes the units to the chosen directory."""

    def test_launchd_writes_two_plists(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`--install-schedule launchd` writes both plists and exits 0."""
        monkeypatch.setattr(
            "creek.cli._load_config_for_vault", lambda _v: CreekConfig()
        )
        out = tmp_path / "out"
        result = runner.invoke(
            app,
            [
                "sync",
                "--install-schedule",
                "launchd",
                "--schedule-out-dir",
                str(out),
                "--vault",
                str(tmp_path / "v"),
            ],
        )
        assert result.exit_code == 0, result.output
        assert (out / "com.creek.sync.tier-a.plist").exists()
        assert (out / "com.creek.sync.tier-b.plist").exists()
        # The written content targets the chosen vault.
        plist = (out / "com.creek.sync.tier-a.plist").read_text(encoding="utf-8")
        assert f"<string>{tmp_path / 'v'}</string>" in plist

    def test_default_output_dir(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Without --schedule-out-dir, launchd plists land in ~/Library/LaunchAgents."""
        monkeypatch.setattr(
            "creek.cli._load_config_for_vault", lambda _v: CreekConfig()
        )
        home = tmp_path / "home"
        monkeypatch.setattr("pathlib.Path.home", lambda: home)
        result = runner.invoke(
            app,
            ["sync", "--install-schedule", "launchd", "--vault", str(tmp_path / "v")],
        )
        assert result.exit_code == 0, result.output
        assert (
            home / "Library" / "LaunchAgents" / "com.creek.sync.tier-a.plist"
        ).exists()

    def test_systemd_writes_four_units(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`--install-schedule systemd` writes the service+timer pair per tier."""
        monkeypatch.setattr(
            "creek.cli._load_config_for_vault", lambda _v: CreekConfig()
        )
        out = tmp_path / "out"
        result = runner.invoke(
            app,
            [
                "sync",
                "--install-schedule",
                "systemd",
                "--schedule-out-dir",
                str(out),
                "--vault",
                str(tmp_path / "v"),
            ],
        )
        assert result.exit_code == 0, result.output
        for fname in (
            "creek-sync-tier-a.service",
            "creek-sync-tier-a.timer",
            "creek-sync-tier-b.service",
            "creek-sync-tier-b.timer",
        ):
            assert (out / fname).exists()

    def test_invalid_host_errors(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An unknown host exits non-zero."""
        monkeypatch.setattr(
            "creek.cli._load_config_for_vault", lambda _v: CreekConfig()
        )
        result = runner.invoke(
            app,
            ["sync", "--install-schedule", "windows", "--vault", str(tmp_path / "v")],
        )
        assert result.exit_code != 0
