"""Schedule-emitter tests for ``creek sync --install-schedule`` (#679, #1336).

Renders launchd / systemd / cron artifacts parametrised by the config cadence,
and verifies the emitted units are valid and embed the right ``creek sync``
command (Tier A never contains link/index — R6).

Validity is checked per host, and not uniformly. The launchd plists are parsed
as XML. The systemd and cron artifacts are checked against a test-local model
of those schedulers' own step semantics (see :class:`TestCalendarExpander`)
rather than being string-matched against the renderer's own formula — string
equality would pass just as happily for a spec the scheduler refuses to load.

Calibration provenance for that model: every parse verdict and elapse sequence
asserted below was *observed*, not derived — systemd 257.13-1~deb13u1 via
``systemd-analyze calendar --iterations=N --base-time="2026-01-01 00:00:00
UTC"``, and vixie cron 3.0pl1-197 via ``crontab -``, both on Debian trixie,
verified 2026-08-09. The transcripts live in :data:`_RECORDED_ELAPSES` and the
recorded parse failures in :data:`_SYSTEMD_REJECTS`.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from itertools import pairwise
from pathlib import Path

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from creek.cli import app
from creek.config import CreekConfig, SyncConfig
from creek.sync import (
    render_crontab,
    render_launchd_plists,
    render_systemd_units,
)

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


# ---- cadence model: calibrated systemd/cron semantics (#1336) -----------
#
# The renderers emit *step* specs (``*:0/N``, ``0/H:00:00``, ``*/N``). Both
# systemd and cron restart the step at each field boundary, so a step that
# does not divide its field silently produces one short interval per cycle:
# ``*:0/45`` fires at :00 and :45 of every hour, never every 45 minutes. The
# helpers below model that boundary behaviour directly and contain no
# divisibility test of their own, so a test built on them cannot decay into a
# restatement of whatever rule ``schedule.py`` happens to implement.

_MINUTES_PER_HOUR = 60
_HOURS_PER_DAY = 24
_MINUTES_PER_DAY = _MINUTES_PER_HOUR * _HOURS_PER_DAY
_MAX_MINUTE = _MINUTES_PER_HOUR - 1
_MAX_HOUR = _HOURS_PER_DAY - 1

_MINUTE_ONLY_SPEC = re.compile(r"^\*:([^:]+)$")
_DATE_SPEC = re.compile(r"^\*-\*-\* ([^:]+):([^:]+):00$")

# Specs real systemd 257 refuses to parse ("Failed to parse calendar
# specification ...: Invalid argument"), after which the timer unit itself
# refuses to load ("Timer unit lacks value setting. Refusing."). The renderer
# must never emit a member of this set, at any cadence.
_SYSTEMD_REJECTS = frozenset(
    {
        "*:0/60",
        "*:0/90",
        "*:0/120",
        "*:0/1440",
        "*-*-* 0/24:00:00",
    }
)

# Recorded ``systemd-analyze calendar --iterations=N
# --base-time="2026-01-01 00:00:00 UTC"`` output, transcribed verbatim as the
# wall-clock times it printed. This is the calibration ground truth for the
# expander: if the model disagrees with a row, the model is wrong, not the row.
# Note the hour-step rows, which run past midnight — 0/5 lands on 00:00 after
# 20:00 (a four-hour gap) and 0/7 after 21:00 (three hours).
_RECORDED_ELAPSES: dict[str, str] = {
    "*:0/12": (
        "00:12 00:24 00:36 00:48 01:00 01:12 01:24 01:36 01:48 02:00 02:12 02:24 02:36"
    ),
    "*:0/15": (
        "00:15 00:30 00:45 01:00 01:15 01:30 01:45 02:00 02:15 02:30 02:45 03:00 03:15"
    ),
    "*:0/25": (
        "00:25 00:50 01:00 01:25 01:50 02:00 02:25 02:50 03:00 03:25 03:50 04:00 04:25"
    ),
    "*:0/30": (
        "00:30 01:00 01:30 02:00 02:30 03:00 03:30 04:00 04:30 05:00 05:30 06:00 06:30"
    ),
    "*:0/40": (
        "00:40 01:00 01:40 02:00 02:40 03:00 03:40 04:00 04:40 05:00 05:40 06:00 06:40"
    ),
    "*:0/45": (
        "00:45 01:00 01:45 02:00 02:45 03:00 03:45 04:00 04:45 05:00 05:45 06:00 06:45"
    ),
    "*:0/50": (
        "00:50 01:00 01:50 02:00 02:50 03:00 03:50 04:00 04:50 05:00 05:50 06:00 06:50"
    ),
    "*:0/59": (
        "00:59 01:00 01:59 02:00 02:59 03:00 03:59 04:00 04:59 05:00 05:59 06:00 06:59"
    ),
    "*-*-* 0/1:00:00": "01:00 02:00 03:00 04:00 05:00",
    "*-*-* 0/2:00:00": "02:00 04:00 06:00 08:00 10:00",
    "*-*-* 0/3:00:00": "03:00 06:00 09:00 12:00 15:00",
    "*-*-* 0/5:00:00": "05:00 10:00 15:00 20:00 00:00",
    "*-*-* 0/7:00:00": "07:00 14:00 21:00 00:00",
}

# Readable spot-checks; the exhaustive sweep below covers 1..1440 as well.
_SYSTEMD_SWEEP = (
    1,
    5,
    15,
    25,
    30,
    40,
    45,
    50,
    59,
    60,
    90,
    120,
    180,
    240,
    300,
    720,
    1440,
    2880,
)

# Cadences a vixie crontab can express exactly, and the exact line each one
# must produce. Everything else is unrepresentable and must raise.
_EXPRESSIBLE_CRON: dict[int, str] = {
    1: "*/1 * * * *",
    2: "*/2 * * * *",
    3: "*/3 * * * *",
    4: "*/4 * * * *",
    5: "*/5 * * * *",
    6: "*/6 * * * *",
    10: "*/10 * * * *",
    12: "*/12 * * * *",
    15: "*/15 * * * *",
    20: "*/20 * * * *",
    30: "*/30 * * * *",
    60: "0 */1 * * *",
    120: "0 */2 * * *",
    180: "0 */3 * * *",
    240: "0 */4 * * *",
    360: "0 */6 * * *",
    480: "0 */8 * * *",
    720: "0 */12 * * *",
    1440: "0 0 * * *",
}
_EXPRESSIBLE_MINUTES = frozenset(_EXPRESSIBLE_CRON)
_INEXPRESSIBLE_MINUTES = (7, 45, 59, 90, 100, 2880)

_SYSTEMD_UNIT_FILENAMES = (
    "creek-sync-tier-a.service",
    "creek-sync-tier-a.timer",
    "creek-sync-tier-b.service",
    "creek-sync-tier-b.timer",
)


def _calendar_fields(spec: str) -> tuple[str, str]:
    """Split a systemd calendar *spec* into its raw (hour, minute) field text.

    Args:
        spec: An ``OnCalendar=`` value, either ``*:<minute>`` or
            ``*-*-* <hour>:<minute>:00``.

    Returns:
        The un-expanded hour and minute field text.

    Raises:
        ValueError: If *spec* has neither shape. A renderer emitting anything
            else is itself the defect, so this surfaces loudly.
    """
    minute_only = _MINUTE_ONLY_SPEC.match(spec)
    if minute_only is not None:
        return "*", minute_only.group(1)
    dated = _DATE_SPEC.match(spec)
    if dated is not None:
        return dated.group(1), dated.group(2)
    msg = f"calendar spec outside the calibrated model: {spec!r}"
    raise ValueError(msg)


def _field_values(field: str, maximum: int) -> list[int]:
    """Expand one systemd calendar *field* into the values it matches.

    Models systemd's repetition semantics literally: ``start/step`` matches
    ``start, start + step, ...`` up to the field's own maximum, and then the
    counter restarts at the next boundary of the enclosing field. No
    divisibility rule is applied or assumed anywhere in here.

    Args:
        field: Raw field text — ``*``, ``start/step``, or a literal integer.
        maximum: The largest legal value for this field (59 or 23).

    Returns:
        The matching values, ascending.
    """
    if field == "*":
        return list(range(maximum + 1))
    start_text, sep, step_text = field.partition("/")
    if sep:
        return list(range(int(start_text), maximum + 1, int(step_text)))
    return [int(field)]


def _field_accepted(field: str, maximum: int) -> bool:
    """Return whether systemd accepts one calendar *field* at all.

    A repetition step larger than the field's own maximum is rejected
    outright (``*:0/60`` and ``0/24:00:00`` are both "Invalid argument").
    This is a wholly separate failure mode from a step that parses but keeps
    the wrong cadence, which is why the suite checks both.

    Args:
        field: Raw field text — ``*``, ``start/step``, or a literal integer.
        maximum: The largest legal value for this field (59 or 23).

    Returns:
        True if real systemd parses the field.
    """
    if field == "*":
        return True
    start_text, sep, step_text = field.partition("/")
    if sep:
        start, step = int(start_text), int(step_text)
        return 0 <= start <= maximum and 1 <= step <= maximum
    return 0 <= int(field) <= maximum


def _systemd_accepts(spec: str) -> bool:
    """Return whether real systemd parses the whole calendar *spec*.

    Args:
        spec: An ``OnCalendar=`` value.

    Returns:
        True if every field is within systemd's limits.
    """
    hour_field, minute_field = _calendar_fields(spec)
    hour_ok = _field_accepted(hour_field, _MAX_HOUR)
    minute_ok = _field_accepted(minute_field, _MAX_MINUTE)
    return hour_ok and minute_ok


def _calendar_fire_minutes(spec: str) -> list[int]:
    """Return the minutes-of-day at which *spec* fires, over one day.

    Args:
        spec: An ``OnCalendar=`` value.

    Returns:
        Ascending minutes-of-day (0..1439).
    """
    hour_field, minute_field = _calendar_fields(spec)
    hours = _field_values(hour_field, _MAX_HOUR)
    minutes = _field_values(minute_field, _MAX_MINUTE)
    return sorted(h * _MINUTES_PER_HOUR + m for h in hours for m in minutes)


def _calendar_gaps(spec: str) -> list[int]:
    """Return the intervals between consecutive firings of *spec*, in minutes.

    The day is closed into a ring — the last gap runs from the final firing of
    one day to the first firing of the next. Omitting that midnight wrap is
    the single easiest way to make this whole suite vacuous: without it
    ``*-*-* 0/5:00:00`` looks like a flawless five-hour cadence across
    00,05,10,15,20 and the four-hour hole at midnight never shows up.

    Args:
        spec: An ``OnCalendar=`` value.

    Returns:
        One gap per firing (the count equals the number of daily firings).
    """
    fires = _calendar_fire_minutes(spec)
    ring = [*fires, fires[0] + _MINUTES_PER_DAY]
    return [later - earlier for earlier, later in pairwise(ring)]


def _clock(minute_of_day: int) -> str:
    """Format a (possibly multi-day) minute offset as ``HH:MM``.

    Args:
        minute_of_day: Minutes since midnight of the base day.

    Returns:
        The wall-clock time as ``HH:MM``.
    """
    wrapped = minute_of_day % _MINUTES_PER_DAY
    return f"{wrapped // _MINUTES_PER_HOUR:02d}:{wrapped % _MINUTES_PER_HOUR:02d}"


def _calendar_elapses(spec: str, count: int) -> list[str]:
    """Return the first *count* firings of *spec* after 00:00, as ``HH:MM``.

    Expands two full days so the sequence crosses midnight, mirroring
    ``systemd-analyze calendar --iterations=N --base-time=...``.

    Args:
        spec: An ``OnCalendar=`` value.
        count: How many firings to return.

    Returns:
        Wall-clock times, in order.
    """
    fires = _calendar_fire_minutes(spec)
    two_days = [*fires, *(m + _MINUTES_PER_DAY for m in fires)]
    return [_clock(m) for m in two_days if m > 0][:count]


def _cron_to_calendar(schedule: str) -> str:
    """Translate a five-field Tier-A cron *schedule* into its systemd spec.

    cron restarts its step at the field boundary exactly as systemd does
    (``*/45`` fires at :00 and :45, the same wrong cadence), so the calendar
    expander is a faithful model of the cron line too.

    Args:
        schedule: The first five whitespace-separated cron fields.

    Returns:
        The equivalent ``OnCalendar=`` value.
    """
    minute_field, hour_field = schedule.split()[:2]
    if minute_field.startswith("*/"):
        return f"*:0/{minute_field[2:]}"
    if hour_field.startswith("*/"):
        return f"*-*-* 0/{hour_field[2:]}:00:00"
    return f"*-*-* {int(hour_field):02d}:00:00"


def _cron_tier_a_schedule(cron: str) -> str:
    """Return the five schedule fields of the rendered Tier-A crontab line.

    Args:
        cron: A rendered crontab.

    Returns:
        The Tier-A schedule, e.g. ``*/30 * * * *``.

    Raises:
        AssertionError: If the rendered crontab has no Tier-A line at all.
    """
    for line in cron.splitlines():
        if not line.startswith("#") and "--tier A" in line:
            return " ".join(line.split()[:5])
    msg = f"no Tier-A line in the rendered crontab:\n{cron}"
    raise AssertionError(msg)


def _directive(unit: str, key: str) -> str | None:
    """Return the value of the ``key=`` directive in *unit*, or None.

    Only line-initial directives count, so a ``#`` comment mentioning
    ``OnCalendar`` never masquerades as one.

    Args:
        unit: A rendered systemd unit file.
        key: The directive name, without the ``=``.

    Returns:
        The directive's value, or None when the directive is absent.
    """
    prefix = f"{key}="
    for line in unit.splitlines():
        if line.startswith(prefix):
            return line[len(prefix) :]
    return None


def _comment_text(unit: str) -> str:
    """Return every ``#`` comment line in *unit*, joined and lowercased.

    Args:
        unit: A rendered systemd unit file.

    Returns:
        The comment text, lowercased for substring checks.
    """
    comments = [line for line in unit.splitlines() if line.startswith("#")]
    return " ".join(comments).lower()


def _unrepresentable_error() -> type[ValueError]:
    """Return ``creek.sync.UnrepresentableCadenceError``, imported lazily.

    The import is deferred so this module still *collects* while the symbol
    does not yet exist (Gate 1 RED): only the tests that need it fail, and the
    pre-existing tests above keep running. Once :mod:`creek.sync` exports it,
    this is an ordinary import.

    Returns:
        The exception type cron raises for an unrepresentable cadence.
    """
    from creek.sync import UnrepresentableCadenceError

    return UnrepresentableCadenceError


def _cron_refuses(minutes: int, error: type[ValueError]) -> bool:
    """Return whether ``render_crontab`` refuses a *minutes* cadence.

    Args:
        minutes: The Tier-A cadence to render.
        error: The unrepresentable-cadence type, resolved once by the caller so
            a missing symbol surfaces as one ImportError rather than 1421.

    Returns:
        True when the renderer raised rather than emitting a line.
    """
    try:
        render_crontab(vault=_VAULT, tier_a_minutes=minutes)
    except error:
        return True
    return False


def _assert_calendar_branch(timer: str, spec: str, minutes: int) -> None:
    """Assert a calendar timer both loads and keeps a *minutes* cadence.

    Two independent clauses, because either one alone passes a broken spec:
    a uniform-gap check alone blesses ``*:0/60`` (the model expands it to a
    single firing per hour, a perfectly uniform 60-minute cadence) even though
    systemd refuses to load it.

    Args:
        timer: The rendered Tier-A timer unit.
        spec: The ``OnCalendar=`` value it carries.
        minutes: The configured Tier-A cadence.
    """
    assert _systemd_accepts(spec), (
        f"tier_a_minutes={minutes} emitted OnCalendar={spec!r}, which real "
        "systemd 257 refuses to parse (the timer unit then fails to load)"
    )
    gaps = _calendar_gaps(spec)
    assert gaps == [minutes] * len(gaps), (
        f"tier_a_minutes={minutes} emitted OnCalendar={spec!r}, which parses "
        f"but fires on gaps {sorted(set(gaps))} minutes, not every {minutes}"
    )
    assert _directive(timer, "Persistent") == "true", (
        f"tier_a_minutes={minutes}: a calendar timer must set Persistent=true "
        "so a tick missed while the host slept still runs"
    )
    assert _directive(timer, "OnBootSec") is None
    assert _directive(timer, "OnUnitActiveSec") is None


def _assert_monotonic_branch(timer: str, minutes: int) -> None:
    """Assert a monotonic timer carries both directives and neither inert one.

    Args:
        timer: The rendered Tier-A timer unit.
        minutes: The configured Tier-A cadence.
    """
    assert _directive(timer, "OnBootSec") == f"{minutes}min", (
        f"tier_a_minutes={minutes}: a monotonic timer needs OnBootSec, or it "
        "never starts until the unit is activated by hand"
    )
    assert _directive(timer, "OnUnitActiveSec") == f"{minutes}min", (
        f"tier_a_minutes={minutes}: a monotonic timer needs OnUnitActiveSec, "
        "or it fires exactly once per boot"
    )
    assert _directive(timer, "OnCalendar") is None
    assert _directive(timer, "Persistent") is None, (
        f"tier_a_minutes={minutes}: Persistent= is inert on a monotonic timer "
        "and must not be emitted, because it promises catch-up it cannot give"
    )


def _assert_tier_a_cadence_is_honest(timer: str, minutes: int) -> None:
    """Assert the Tier-A timer keeps *minutes* honestly, on exactly one branch.

    Either it is a calendar timer whose spec systemd accepts *and* whose
    expanded firings are uniformly *minutes* apart (midnight wrap included),
    or it is a monotonic timer. Never both, never neither.

    Args:
        timer: The rendered Tier-A timer unit.
        minutes: The configured Tier-A cadence.
    """
    calendar = _directive(timer, "OnCalendar")
    monotonic = _directive(timer, "OnUnitActiveSec")
    assert (calendar is None) != (monotonic is None), (
        f"tier_a_minutes={minutes}: the timer must be exactly one of calendar "
        f"or monotonic; got OnCalendar={calendar!r}, "
        f"OnUnitActiveSec={monotonic!r}"
    )
    if calendar is not None:
        _assert_calendar_branch(timer, calendar, minutes)
    else:
        _assert_monotonic_branch(timer, minutes)


def _install_via_cli(
    monkeypatch: pytest.MonkeyPatch,
    *,
    host: str,
    out: Path,
    vault: Path,
    minutes: int,
) -> str:
    """Run ``creek sync --install-schedule`` at *minutes*; return its stdout.

    Asserts exit 0: an irregular cadence is a supported configuration (systemd
    falls back to a monotonic timer), not a CLI failure.

    Args:
        monkeypatch: pytest's monkeypatch fixture.
        host: ``launchd`` or ``systemd``.
        out: Directory the units are written to.
        vault: The vault the units target.
        minutes: The Tier-A cadence to configure.

    Returns:
        The captured CLI output.
    """
    config = CreekConfig(sync=SyncConfig(tier_a_interval_minutes=minutes))
    monkeypatch.setattr("creek.cli._load_config_for_vault", lambda _v: config)
    result = runner.invoke(
        app,
        [
            "sync",
            "--install-schedule",
            host,
            "--schedule-out-dir",
            str(out),
            "--vault",
            str(vault),
        ],
    )
    assert result.exit_code == 0, result.output
    return result.output


def _tier_a_timer_text(out: Path) -> str:
    """Read the Tier-A timer unit written into *out*.

    Args:
        out: The ``--schedule-out-dir`` the CLI wrote to.

    Returns:
        The timer file's contents.
    """
    return (out / "creek-sync-tier-a.timer").read_text(encoding="utf-8")


# ---- the expander itself, pinned to observed scheduler behaviour --------


class TestCalendarExpander:
    """The test-local calendar model reproduces what real systemd printed.

    This class is the calibration step. Everything downstream trusts the
    expander, so the expander is pinned to recorded ``systemd-analyze
    calendar`` output rather than to anything ``schedule.py`` believes.
    """

    @pytest.mark.parametrize(
        ("spec", "expected"),
        [
            (
                "*:0/45",
                ["00:45", "01:00", "01:45", "02:00", "02:45", "03:00"],
            ),
            (
                "*:0/59",
                ["00:59", "01:00", "01:59", "02:00", "02:59", "03:00"],
            ),
            (
                "*:0/50",
                ["00:50", "01:00", "01:50", "02:00", "02:50", "03:00"],
            ),
            (
                "*:0/25",
                ["00:25", "00:50", "01:00", "01:25", "01:50", "02:00"],
            ),
            (
                "*-*-* 0/5:00:00",
                ["05:00", "10:00", "15:00", "20:00", "00:00", "05:00"],
            ),
            (
                "*-*-* 0/7:00:00",
                ["07:00", "14:00", "21:00", "00:00", "07:00", "14:00"],
            ),
        ],
    )
    def test_elapses_match_recorded_systemd_output(
        self, spec: str, expected: list[str]
    ) -> None:
        """The model reproduces the recorded elapse sequence exactly."""
        assert _calendar_elapses(spec, len(expected)) == expected

    @pytest.mark.parametrize(
        ("spec", "gap"),
        [
            ("*:0/15", 15),
            ("*:0/30", 30),
            ("*-*-* 0/1:00:00", 60),
            ("*-*-* 0/2:00:00", 120),
            ("*-*-* 0/3:00:00", 180),
        ],
    )
    def test_divisor_specs_are_uniform_all_day(self, spec: str, gap: int) -> None:
        """A step that divides its field gives one gap value, all day long."""
        gaps = _calendar_gaps(spec)
        assert gaps == [gap] * (_MINUTES_PER_DAY // gap)
        assert sum(gaps) == _MINUTES_PER_DAY

    @pytest.mark.parametrize(
        ("spec", "cycle"),
        [
            ("*:0/25", [25, 25, 10]),
            ("*:0/40", [40, 20]),
            ("*:0/45", [45, 15]),
            ("*:0/50", [50, 10]),
            ("*:0/59", [59, 1]),
        ],
    )
    def test_non_divisor_specs_repeat_a_short_interval(
        self, spec: str, cycle: list[int]
    ) -> None:
        """A step that does not divide the hour re-runs a short tail hourly."""
        assert _calendar_gaps(spec) == cycle * _HOURS_PER_DAY

    @pytest.mark.parametrize(
        ("spec", "gaps"),
        [
            ("*-*-* 0/5:00:00", [300, 300, 300, 300, 240]),
            ("*-*-* 0/7:00:00", [420, 420, 420, 180]),
        ],
    )
    def test_midnight_wrap_is_measured(self, spec: str, gaps: list[int]) -> None:
        """The hour step's short interval lives at midnight — and is counted.

        The second assertion is the point: every gap *except* the wrap is
        identical, so an expander that stopped at 23:59 would report a
        flawless cadence and quietly bless the bug.
        """
        assert _calendar_gaps(spec) == gaps
        assert len(set(gaps[:-1])) == 1
        assert gaps[-1] != gaps[0]

    @pytest.mark.parametrize(
        ("spec", "accepted"),
        [
            ("*:0/1", True),
            ("*:0/15", True),
            ("*:0/59", True),
            ("*:0/60", False),
            ("*:0/90", False),
            ("*:0/120", False),
            ("*:0/1440", False),
            ("*-*-* 0/1:00:00", True),
            ("*-*-* 0/23:00:00", True),
            ("*-*-* 0/24:00:00", False),
            ("*-*-* 00:00:00", True),
            ("*-*-* 03:00:00", True),
        ],
    )
    def test_parse_verdicts_match_recorded_systemd(
        self, spec: str, accepted: bool
    ) -> None:
        """Acceptance matches what ``systemd-analyze calendar`` reported."""
        assert _systemd_accepts(spec) is accepted

    def test_uniform_gaps_alone_do_not_prove_acceptance(self) -> None:
        """Why faithfulness needs a parse clause beside it.

        ``*:0/60`` and ``0/24:00:00`` both expand to perfectly uniform
        cadences under the model, yet real systemd rejects both outright. A
        suite that only checked gap uniformity would pass N=60 and N=1440
        while shipping a timer that will not load.
        """
        assert _calendar_gaps("*:0/60") == [60] * _HOURS_PER_DAY
        assert _systemd_accepts("*:0/60") is False
        assert _calendar_gaps("*-*-* 0/24:00:00") == [_MINUTES_PER_DAY]
        assert _systemd_accepts("*-*-* 0/24:00:00") is False

    @pytest.mark.parametrize(("spec", "expected"), sorted(_RECORDED_ELAPSES.items()))
    def test_recorded_elapse_sequence_is_reproduced(
        self, spec: str, expected: str
    ) -> None:
        """The model reproduces every transcribed ``systemd-analyze`` run.

        The whole suite rests on this expander, so it is pinned to the recorded
        output of a real scheduler rather than to any formula. A row like
        ``*:0/40`` firing at 00:40 and then 01:00 — twenty minutes later, not
        forty — is the defect stated in the scheduler's own words.
        """
        wanted = expected.split()
        assert _calendar_elapses(spec, len(wanted)) == wanted

    def test_only_nineteen_cadences_have_a_faithful_calendar_spec(self) -> None:
        """Brute-force the grammar: the expressible set is exactly those 19.

        Nothing here divides anything. Every spec systemd's grammar admits is
        expanded, kept only if all its gaps are equal, and the surviving
        cadences are compared against the table the rest of the suite uses.
        That makes the table *evidence* — and, just as importantly, proves the
        refusal set is not over-broad: no cadence cron could have expressed is
        being rejected out of caution.
        """
        specs = (
            [f"*:0/{step}" for step in range(1, _MINUTES_PER_HOUR)]
            + [f"*-*-* 0/{step}:00:00" for step in range(1, _HOURS_PER_DAY)]
            + [f"*-*-* {hour:02d}:00:00" for hour in range(_HOURS_PER_DAY)]
        )
        assert all(_systemd_accepts(spec) for spec in specs)
        achievable = {
            _calendar_gaps(spec)[0]
            for spec in specs
            if len(set(_calendar_gaps(spec))) == 1
        }
        assert achievable == set(_EXPRESSIBLE_MINUTES)


# ---- systemd: every cadence is either faithful or monotonic -------------


class TestSystemdCadenceFaithfulness:
    """The Tier-A timer must never promise a cadence systemd will not keep."""

    @pytest.mark.parametrize("minutes", _SYSTEMD_SWEEP)
    def test_swept_cadence_is_honest(self, minutes: int) -> None:
        """Each sampled cadence loads and keeps its interval, or goes monotonic."""
        units = render_systemd_units(vault=_VAULT, tier_a_minutes=minutes)
        _assert_tier_a_cadence_is_honest(units.tier_a_timer, minutes)

    def test_every_cadence_from_one_to_one_day_is_honest(self) -> None:
        """Totality: no value of ``tier_a_interval_minutes`` in 1..1440 lies."""
        for minutes in range(1, _MINUTES_PER_DAY + 1):
            units = render_systemd_units(vault=_VAULT, tier_a_minutes=minutes)
            _assert_tier_a_cadence_is_honest(units.tier_a_timer, minutes)

    def test_renderer_never_emits_a_spec_systemd_rejects(self) -> None:
        """No cadence in 1..1440 produces a known-unloadable OnCalendar."""
        emitted: set[str] = set()
        for minutes in range(1, _MINUTES_PER_DAY + 1):
            units = render_systemd_units(vault=_VAULT, tier_a_minutes=minutes)
            spec = _directive(units.tier_a_timer, "OnCalendar")
            if spec is not None:
                emitted.add(spec)
        offenders = emitted & _SYSTEMD_REJECTS
        assert not offenders, f"emitted specs systemd refuses to load: {offenders}"

    @pytest.mark.parametrize("minutes", [1, 5, 15, 30, 60, 120, 720, 1440])
    def test_representable_cadence_uses_a_persistent_calendar_timer(
        self, minutes: int
    ) -> None:
        """A cadence systemd *can* express keeps the self-healing calendar form.

        Guards against "solving" the bug by making every cadence monotonic,
        which would silently drop missed-tick catch-up for the common cases.
        """
        units = render_systemd_units(vault=_VAULT, tier_a_minutes=minutes)
        assert _directive(units.tier_a_timer, "OnCalendar") is not None
        assert _directive(units.tier_a_timer, "Persistent") == "true"

    @pytest.mark.parametrize("minutes", [7, 25, 45, 50, 59, 90, 100, 300, 2880])
    def test_irregular_cadence_uses_a_monotonic_timer(self, minutes: int) -> None:
        """A cadence no calendar spec can express becomes a monotonic timer."""
        units = render_systemd_units(vault=_VAULT, tier_a_minutes=minutes)
        assert _directive(units.tier_a_timer, "OnCalendar") is None
        _assert_monotonic_branch(units.tier_a_timer, minutes)

    @pytest.mark.parametrize("minutes", [45, 90, 300, 2880])
    def test_monotonic_timer_discloses_the_lost_catch_up(self, minutes: int) -> None:
        """The monotonic timer says in-file that missed ticks are not caught up."""
        units = render_systemd_units(vault=_VAULT, tier_a_minutes=minutes)
        comments = _comment_text(units.tier_a_timer)
        assert comments, (
            f"tier_a_minutes={minutes}: a monotonic timer must carry a # "
            "comment explaining the lost catch-up"
        )
        assert "missed" in comments
        assert "catch-up" in comments

    def test_tier_b_timer_is_byte_identical_at_every_cadence(self) -> None:
        """Tier B is correct today and the Tier-A refactor must not touch it."""
        baseline = render_systemd_units(
            vault=_VAULT, tier_a_minutes=30, tier_b_hour=3
        ).tier_b_timer
        assert "OnCalendar=*-*-* 03:00:00" in baseline
        assert "Persistent=true" in baseline
        for minutes in range(1, _MINUTES_PER_DAY + 1):
            units = render_systemd_units(
                vault=_VAULT, tier_a_minutes=minutes, tier_b_hour=3
            )
            assert units.tier_b_timer == baseline, (
                f"tier_a_minutes={minutes} changed the Tier-B timer"
            )

    @pytest.mark.parametrize("minutes", sorted(_EXPRESSIBLE_CRON))
    def test_every_expressible_cadence_keeps_the_calendar_form(
        self, minutes: int
    ) -> None:
        """All nineteen, not just the popular ones, keep Persistent catch-up.

        ``test_only_nineteen_cadences_have_a_faithful_calendar_spec`` proves a
        faithful spec exists for exactly these cadences, so sending any of them
        to the monotonic branch would surrender missed-tick catch-up that was
        never in question — a silent regression at, say, six hours.
        """
        units = render_systemd_units(vault=_VAULT, tier_a_minutes=minutes)
        spec = _directive(units.tier_a_timer, "OnCalendar")
        assert spec is not None, (
            f"tier_a_minutes={minutes} has a faithful calendar spec available "
            "and must not fall back to a monotonic timer"
        )
        _assert_calendar_branch(units.tier_a_timer, spec, minutes)

    def test_every_monotonic_timer_discloses_the_lost_catch_up(self) -> None:
        """Totality for the disclosure: no silent monotonic timer in 1..1440.

        The fallback is honest only if it says what it costs. Checking a
        handful of sampled cadences would leave most of the branch free to ship
        a bare timer that quietly stops catching up after a suspend.
        """
        silent: list[int] = []
        for minutes in range(1, _MINUTES_PER_DAY + 1):
            timer = render_systemd_units(
                vault=_VAULT, tier_a_minutes=minutes
            ).tier_a_timer
            if _directive(timer, "OnCalendar") is not None:
                continue
            comments = _comment_text(timer)
            if "missed" not in comments or "catch-up" not in comments:
                silent.append(minutes)
        assert not silent, (
            f"{len(silent)} monotonic timers never mention the missed-tick "
            f"catch-up they cannot provide, e.g. {silent[:5]}"
        )

    @pytest.mark.parametrize("minutes", _SYSTEMD_SWEEP)
    def test_timer_still_names_its_service_and_install_target(
        self, minutes: int
    ) -> None:
        """Whichever branch it takes, the timer must still fire Tier A.

        A timer that loses ``Unit=`` or ``[Install] WantedBy=`` parses fine and
        does nothing — the worst possible outcome for a fix whose whole point
        is that the schedule actually runs.
        """
        units = render_systemd_units(vault=_VAULT, tier_a_minutes=minutes)
        assert _directive(units.tier_a_timer, "Unit") == "creek-sync-tier-a.service"
        assert _directive(units.tier_a_timer, "WantedBy") == "timers.target"


# ---- cron: exact lines, or a refusal that helps -------------------------


class TestCronCadence:
    """The crontab is either exactly right or an explicit, useful refusal."""

    @pytest.mark.parametrize(("minutes", "schedule"), sorted(_EXPRESSIBLE_CRON.items()))
    def test_expressible_cadence_emits_the_exact_line(
        self, minutes: int, schedule: str
    ) -> None:
        """Each cron-expressible cadence renders its one correct schedule."""
        cron = render_crontab(vault=_VAULT, tier_a_minutes=minutes, tier_b_hour=3)
        assert _cron_tier_a_schedule(cron) == schedule
        assert f'{schedule} creek sync --tier A --vault "{_VAULT}"' in cron

    @pytest.mark.parametrize("minutes", sorted(_EXPRESSIBLE_CRON))
    def test_expressible_cron_line_keeps_its_cadence(self, minutes: int) -> None:
        """The emitted cron line actually fires every *minutes*, wrap included."""
        cron = render_crontab(vault=_VAULT, tier_a_minutes=minutes, tier_b_hour=3)
        spec = _cron_to_calendar(_cron_tier_a_schedule(cron))
        gaps = _calendar_gaps(spec)
        assert gaps == [minutes] * len(gaps), (
            f"tier_a_minutes={minutes} emitted a cron line equivalent to "
            f"{spec!r}, which fires on gaps {sorted(set(gaps))}"
        )

    @pytest.mark.parametrize("minutes", sorted(_EXPRESSIBLE_CRON))
    def test_no_cron_step_exceeds_its_field_maximum(self, minutes: int) -> None:
        """No emitted step trips vixie's 'Step size N higher than maximum'."""
        schedule = _cron_tier_a_schedule(
            render_crontab(vault=_VAULT, tier_a_minutes=minutes)
        )
        minute_field, hour_field = schedule.split()[:2]
        if minute_field.startswith("*/"):
            assert int(minute_field[2:]) <= _MAX_MINUTE, schedule
        if hour_field.startswith("*/"):
            assert int(hour_field[2:]) <= _MAX_HOUR, schedule

    @pytest.mark.parametrize("minutes", sorted(_EXPRESSIBLE_CRON))
    def test_tier_b_cron_line_is_unchanged(self, minutes: int) -> None:
        """The nightly Tier-B line is correct today and stays byte-for-byte."""
        cron = render_crontab(vault=_VAULT, tier_a_minutes=minutes, tier_b_hour=3)
        assert f'0 3 * * * creek sync --tier B --vault "{_VAULT}"' in cron

    @pytest.mark.parametrize("minutes", _INEXPRESSIBLE_MINUTES)
    def test_inexpressible_cadence_raises_a_value_error(self, minutes: int) -> None:
        """cron refuses rather than emitting a line that misfires or is rejected."""
        error = _unrepresentable_error()
        assert issubclass(error, ValueError)
        with pytest.raises(error):
            render_crontab(vault=_VAULT, tier_a_minutes=minutes)

    @pytest.mark.parametrize("minutes", _INEXPRESSIBLE_MINUTES)
    def test_refusal_names_the_value_and_a_real_alternative(self, minutes: int) -> None:
        """The refusal is actionable: the bad cadence, plus a cadence that works."""
        with pytest.raises(_unrepresentable_error()) as excinfo:
            render_crontab(vault=_VAULT, tier_a_minutes=minutes)
        message = str(excinfo.value)
        numbers = {int(token) for token in re.findall(r"\d+", message)}
        assert minutes in numbers, (
            f"the message must name the rejected cadence {minutes}: {message!r}"
        )
        assert numbers & (_EXPRESSIBLE_MINUTES - {minutes}), (
            "the message must offer at least one cron-expressible cadence; "
            f"got {message!r}"
        )

    def test_every_cadence_outside_the_nineteen_is_refused(self) -> None:
        """Totality: the 19 render, and all 1421 others raise.

        Six sampled refusals leave room for an implementation that special-cases
        the probes; this closes the gap the other way round, so a cadence like
        13 or 200 cannot slip out as a crontab vixie would reject wholesale —
        taking the operator's Tier-B line down with it.
        """
        error = _unrepresentable_error()
        candidates = [
            minutes
            for minutes in range(1, _MINUTES_PER_DAY + 1)
            if minutes not in _EXPRESSIBLE_MINUTES
        ]
        leaked = [n for n in candidates if not _cron_refuses(n, error)]
        assert not leaked, (
            f"{len(leaked)} cadences cron cannot express were rendered anyway, "
            f"e.g. {leaked[:5]}"
        )


# ---- CLI: what actually lands on disk -----------------------------------


class TestInstallScheduleCadenceCli:
    """``--install-schedule`` writes honest units at every configured cadence."""

    def test_default_cadence_is_unchanged(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No-regression anchor: the 30-minute default still emits the old units."""
        out = tmp_path / "out"
        output = _install_via_cli(
            monkeypatch,
            host="systemd",
            out=out,
            vault=tmp_path / "v",
            minutes=30,
        )
        for fname in _SYSTEMD_UNIT_FILENAMES:
            assert (out / fname).exists()
        timer = _tier_a_timer_text(out)
        assert _directive(timer, "OnCalendar") == "*:0/30"
        assert _directive(timer, "Persistent") == "true"
        assert _directive(timer, "OnBootSec") is None
        assert "*/30 * * * *" in output

    def test_irregular_cadence_writes_a_monotonic_timer_and_exits_zero(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """N=90: all four units land, cron is explained away, exit stays 0."""
        out = tmp_path / "out"
        output = _install_via_cli(
            monkeypatch,
            host="systemd",
            out=out,
            vault=tmp_path / "v",
            minutes=90,
        )
        for fname in _SYSTEMD_UNIT_FILENAMES:
            assert (out / fname).exists()
        timer = _tier_a_timer_text(out)
        assert _directive(timer, "OnUnitActiveSec") == "90min"
        assert _directive(timer, "OnBootSec") == "90min"
        assert _directive(timer, "OnCalendar") is None
        assert "*/90" not in output, (
            "a cadence cron cannot express must never be printed as a cron "
            "line — vixie rejects the whole crontab, taking Tier B with it"
        )
        assert "0 3 * * *" not in output
        # Assert against the prose only. A bare ``"90" in output`` would be
        # satisfied by a ``pytest-90`` tmp directory inside a printed path --
        # exactly the accidental pass that makes a green gate meaningless --
        # so drop the ``# wrote <path>`` lines first.
        prose = "\n".join(
            ln for ln in output.splitlines() if not ln.startswith("# wrote ")
        )
        assert "cron" in prose.lower(), (
            f"the operator must be told why no crontab was printed:\n{output}"
        )
        assert "90" in prose, (
            f"the explanation must name the offending 90-minute cadence:\n{prose}"
        )
        assert "catch-up" in prose.lower(), (
            f"the monotonic fallback must disclose the lost catch-up:\n{prose}"
        )

    def test_hourly_cadence_uses_the_hour_step_form(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """N=120 becomes an hour-stepped calendar timer, not ``*:0/120``."""
        out = tmp_path / "out"
        output = _install_via_cli(
            monkeypatch,
            host="systemd",
            out=out,
            vault=tmp_path / "v",
            minutes=120,
        )
        timer = _tier_a_timer_text(out)
        assert _directive(timer, "OnCalendar") == "*-*-* 0/2:00:00"
        assert _directive(timer, "Persistent") == "true"
        assert "0 */2 * * *" in output

    def test_daily_cadence_uses_the_literal_midnight_form(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """N=1440 becomes a literal midnight spec; ``0/24`` is unloadable."""
        out = tmp_path / "out"
        output = _install_via_cli(
            monkeypatch,
            host="systemd",
            out=out,
            vault=tmp_path / "v",
            minutes=1440,
        )
        timer = _tier_a_timer_text(out)
        assert _directive(timer, "OnCalendar") == "*-*-* 00:00:00"
        assert "0/24" not in timer
        assert "0 0 * * *" in output

    def test_launchd_is_untouched_by_an_irregular_cadence(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """launchd expresses N=90 exactly, so the config value is not rejected."""
        out = tmp_path / "out"
        _install_via_cli(
            monkeypatch,
            host="launchd",
            out=out,
            vault=tmp_path / "v",
            minutes=90,
        )
        plist = (out / "com.creek.sync.tier-a.plist").read_text(encoding="utf-8")
        assert "<key>StartInterval</key>" in plist
        assert "<integer>5400</integer>" in plist
        assert (out / "com.creek.sync.tier-b.plist").exists()

    @pytest.mark.parametrize("minutes", [30, 90, 120, 1440])
    def test_unit_filenames_are_unchanged_at_every_cadence(
        self, minutes: int, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Same four names at every cadence, so re-installing overwrites cleanly."""
        out = tmp_path / "out"
        _install_via_cli(
            monkeypatch,
            host="systemd",
            out=out,
            vault=tmp_path / "v",
            minutes=minutes,
        )
        written = sorted(p.name for p in out.iterdir())
        assert written == sorted(_SYSTEMD_UNIT_FILENAMES)


# ---- config: the cadence field keeps its current bounds -----------------


class TestSyncCadenceConfigBounds:
    """``tier_a_interval_minutes`` stays ``ge=1`` with no upper bound."""

    @pytest.mark.parametrize("minutes", [1, 45, 59, 90, 1440, 2880])
    def test_irregular_cadences_are_still_accepted(self, minutes: int) -> None:
        """Config must not cap the cadence at 59.

        Capping would be a data-loss fix: it forbids the perfectly reasonable
        "every 90 minutes", which launchd expresses exactly and systemd
        expresses as a monotonic timer. The renderers, not the schema, are
        where the host limits belong.
        """
        config = SyncConfig(tier_a_interval_minutes=minutes)
        assert config.tier_a_interval_minutes == minutes

    @pytest.mark.parametrize("minutes", [0, -1])
    def test_non_positive_cadence_is_still_rejected(self, minutes: int) -> None:
        """The existing ``ge=1`` floor survives the change."""
        with pytest.raises(ValidationError):
            SyncConfig(tier_a_interval_minutes=minutes)

    def test_tier_b_hour_bounds_are_unchanged(self) -> None:
        """Tier B is correct today: 0..23 inclusive, nothing outside."""
        assert SyncConfig(tier_b_hour=0).tier_b_hour == 0
        assert SyncConfig(tier_b_hour=23).tier_b_hour == 23
        with pytest.raises(ValidationError):
            SyncConfig(tier_b_hour=24)


# ---- anti-vacuity: the matrices above are not empty ---------------------


class TestCadenceMatricesAreNotEmpty:
    """An emptied parametrize list silently skips behind a green gate."""

    def test_expressible_cron_matrix_is_complete(self) -> None:
        """Eleven minute-steps, seven hour-steps, one daily line."""
        assert len(_EXPRESSIBLE_CRON) == 19
        lines = list(_EXPRESSIBLE_CRON.values())
        assert sum(1 for line in lines if line.startswith("*/")) == 11
        assert sum(1 for line in lines if line.startswith("0 */")) == 7
        assert lines.count("0 0 * * *") == 1

    def test_inexpressible_matrix_is_populated(self) -> None:
        """At least the six recorded cron-hostile cadences are covered."""
        assert len(_INEXPRESSIBLE_MINUTES) >= 6

    def test_systemd_sweep_is_populated(self) -> None:
        """The readable systemd sweep keeps all eighteen sampled cadences."""
        assert len(_SYSTEMD_SWEEP) == 18

    def test_the_two_cron_matrices_are_disjoint(self) -> None:
        """No cadence is asserted both expressible and inexpressible."""
        assert not _EXPRESSIBLE_MINUTES & set(_INEXPRESSIBLE_MINUTES)

    def test_the_rejection_set_is_populated(self) -> None:
        """The known-unloadable systemd specs are all still listed."""
        assert len(_SYSTEMD_REJECTS) == 5

    def test_the_recorded_elapse_table_is_populated(self) -> None:
        """Thirteen transcribed runs, each with at least four firings.

        Deleting rows here would not fail anything by itself — the calibration
        would simply stop calibrating, and every downstream assertion would
        keep passing against an unchecked model.
        """
        assert len(_RECORDED_ELAPSES) == 13
        assert all(len(row.split()) >= 4 for row in _RECORDED_ELAPSES.values())
        assert sum(1 for spec in _RECORDED_ELAPSES if spec.startswith("*:0/")) == 8


# ---- a cadence below 1 is not a cadence ---------------------------------


class TestNonPositiveCadenceIsRefused:
    """No renderer may emit a unit for a zero or negative interval.

    ``SyncConfig`` bounds the field at ``ge=1``, but the renderers are public
    (``creek.sync`` re-exports all three), so the guard belongs with them too.
    Observed on systemd 257: ``OnBootSec=-5min`` draws *"Failed to parse timer
    value, ignoring"*, which strips the trigger and gets the unit refused —
    the very failure #1336 is about — while ``OnBootSec=0min`` loads and
    re-fires with no delay. Both are units the emitter cannot vouch for.
    """

    @pytest.mark.parametrize("minutes", [0, -1, -5, -1440])
    def test_systemd_refuses(self, minutes: int) -> None:
        """The systemd renderer raises rather than writing a broken timer."""
        with pytest.raises(ValueError, match="tier_a_minutes"):
            render_systemd_units(vault=_VAULT, tier_a_minutes=minutes)

    @pytest.mark.parametrize("minutes", [0, -1, -5, -1440])
    def test_cron_refuses(self, minutes: int) -> None:
        """The cron renderer raises rather than writing a broken line."""
        with pytest.raises(ValueError, match="tier_a_minutes"):
            render_crontab(vault=_VAULT, tier_a_minutes=minutes)

    @pytest.mark.parametrize("minutes", [0, -1, -5, -1440])
    def test_launchd_refuses(self, minutes: int) -> None:
        """launchd is exact for every real cadence, but 0 is not one."""
        with pytest.raises(ValueError, match="tier_a_minutes"):
            render_launchd_plists(vault=_VAULT, tier_a_minutes=minutes)

    def test_one_minute_is_still_allowed(self) -> None:
        """The guard is ``< 1``, not ``< 2``: the smallest real cadence works."""
        units = render_systemd_units(vault=_VAULT, tier_a_minutes=1)
        assert _directive(units.tier_a_timer, "OnCalendar") == "*:0/1"
        assert render_launchd_plists(vault=_VAULT, tier_a_minutes=1).tier_a
        assert render_crontab(vault=_VAULT, tier_a_minutes=1)
