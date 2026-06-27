"""Render launchd / systemd / cron schedule artifacts for ``creek sync`` (#679).

Each renderer is a pure function: given a vault path and the config-tunable
cadence (Tier-A interval in minutes, Tier-B nightly hour), it returns the unit
file contents as strings. The CLI writes them to a host-appropriate location
and prints activation instructions; installing them into the live system is the
operator's manual step (SPEC decision #4 — BUILD BOTH host adapters).

Tier A runs the cheap per-source pass (`creek sync --tier A`); Tier B runs the
nightly global pass (`creek sync --tier B`). The embedded commands never contain
`link`/`index` directly — those run inside Tier B only (R6).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

# The scheduled command. ``creek`` is expected on PATH (or the operator edits
# the absolute path into the emitted unit — noted in docs/sync-scheduling.md).
_CREEK = "creek"


def _tier_command(vault: Path, tier: str) -> str:
    """Return the ``creek sync`` command line for *tier* against *vault*."""
    return f"{_CREEK} sync --tier {tier} --vault {vault}"


@dataclass(frozen=True)
class LaunchdPlists:
    """Rendered launchd plist contents for both tiers (macOS, #679)."""

    tier_a: str
    tier_b: str
    tier_a_filename: str = "com.creek.sync.tier-a.plist"
    tier_b_filename: str = "com.creek.sync.tier-b.plist"


@dataclass(frozen=True)
class SystemdUnits:
    """Rendered systemd service+timer contents for both tiers (Linux, #679)."""

    tier_a_service: str
    tier_a_timer: str
    tier_b_service: str
    tier_b_timer: str
    tier_a_service_filename: str = "creek-sync-tier-a.service"
    tier_a_timer_filename: str = "creek-sync-tier-a.timer"
    tier_b_service_filename: str = "creek-sync-tier-b.service"
    tier_b_timer_filename: str = "creek-sync-tier-b.timer"


def _plist(label: str, command: str, schedule_xml: str) -> str:
    """Render one launchd plist with *schedule_xml* as the cadence element."""
    program_args = "".join(
        f"        <string>{arg}</string>\n" for arg in command.split()
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
        '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        '<plist version="1.0">\n'
        "  <dict>\n"
        "    <key>Label</key>\n"
        f"    <string>{label}</string>\n"
        "    <key>ProgramArguments</key>\n"
        "    <array>\n"
        f"{program_args}"
        "    </array>\n"
        f"{schedule_xml}"
        "    <key>RunAtLoad</key>\n"
        "    <false/>\n"
        "  </dict>\n"
        "</plist>\n"
    )


def render_launchd_plists(
    *,
    vault: Path,
    tier_a_minutes: int = 30,
    tier_b_hour: int = 3,
) -> LaunchdPlists:
    """Render the Tier-A and Tier-B launchd plists for *vault* (#679).

    Tier A uses ``StartInterval`` (the configured minutes, in seconds); Tier B
    uses ``StartCalendarInterval`` at the configured nightly hour.
    """
    tier_a_schedule = (
        f"    <key>StartInterval</key>\n    <integer>{tier_a_minutes * 60}</integer>\n"
    )
    tier_b_schedule = (
        "    <key>StartCalendarInterval</key>\n"
        "    <dict>\n"
        "      <key>Hour</key>\n"
        f"      <integer>{tier_b_hour}</integer>\n"
        "      <key>Minute</key>\n"
        "      <integer>0</integer>\n"
        "    </dict>\n"
    )
    return LaunchdPlists(
        tier_a=_plist(
            "com.creek.sync.tier-a",
            _tier_command(vault, "A"),
            tier_a_schedule,
        ),
        tier_b=_plist(
            "com.creek.sync.tier-b",
            _tier_command(vault, "B"),
            tier_b_schedule,
        ),
    )


def _service(description: str, command: str) -> str:
    """Render a oneshot systemd service that runs *command*."""
    return (
        "[Unit]\n"
        f"Description={description}\n\n"
        "[Service]\n"
        "Type=oneshot\n"
        f"ExecStart={command}\n"
    )


def _timer(description: str, on_calendar: str, unit: str) -> str:
    """Render a systemd timer firing *unit* on the *on_calendar* schedule."""
    return (
        "[Unit]\n"
        f"Description={description}\n\n"
        "[Timer]\n"
        f"OnCalendar={on_calendar}\n"
        "Persistent=true\n"
        f"Unit={unit}\n\n"
        "[Install]\n"
        "WantedBy=timers.target\n"
    )


def render_systemd_units(
    *,
    vault: Path,
    tier_a_minutes: int = 30,
    tier_b_hour: int = 3,
) -> SystemdUnits:
    """Render the Tier-A/B systemd service + timer units for *vault* (#679).

    Tier A fires every ``tier_a_minutes`` (``OnCalendar=*:0/N``); Tier B fires
    nightly at ``tier_b_hour`` (``OnCalendar=*-*-* HH:00:00``). ``Persistent``
    means a missed tick (host asleep) runs on next boot — self-healing.
    """
    units = SystemdUnits(
        tier_a_service=_service(
            "Creek sync (Tier A: pull/ingest/rules-classify)",
            _tier_command(vault, "A"),
        ),
        tier_a_timer=_timer(
            "Creek sync Tier A schedule",
            f"*:0/{tier_a_minutes}",
            "creek-sync-tier-a.service",
        ),
        tier_b_service=_service(
            "Creek sync (Tier B: llm-classify/link/index)",
            _tier_command(vault, "B"),
        ),
        tier_b_timer=_timer(
            "Creek sync Tier B schedule",
            f"*-*-* {tier_b_hour:02d}:00:00",
            "creek-sync-tier-b.service",
        ),
    )
    return units


def render_crontab(
    *,
    vault: Path,
    tier_a_minutes: int = 30,
    tier_b_hour: int = 3,
) -> str:
    """Render a two-line crontab for the Tier-A/B sync passes (#679)."""
    tier_a = f"*/{tier_a_minutes} * * * * {_tier_command(vault, 'A')}"
    tier_b = f"0 {tier_b_hour} * * * {_tier_command(vault, 'B')}"
    return (
        "# Creek sync schedule (crontab) — install with `crontab -e`\n"
        f"{tier_a}\n"
        f"{tier_b}\n"
    )
