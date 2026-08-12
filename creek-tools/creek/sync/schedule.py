"""Render launchd / systemd / cron schedule artifacts for ``creek sync``.

Each renderer is a pure function: given a vault path and the config-tunable
cadence (Tier-A interval in minutes, Tier-B nightly hour), it returns the unit
file contents as strings. The CLI writes them to a host-appropriate location
and prints activation instructions; installing them into the live system is the
operator's manual step (SPEC decision #4 — BUILD BOTH host adapters).

Tier A runs the cheap per-source pass (`creek sync --tier A`); Tier B runs the
nightly global pass (`creek sync --tier B`). The embedded commands never contain
`link`/`index` directly — those run inside Tier B only (R6).

Vault paths may contain spaces (common on macOS, e.g. ``~/Documents/My
Notes``): launchd receives the command as an argument *list* (one ``<string>``
per token, so the path stays whole), while systemd ``ExecStart`` and the cron
line *quote* the path.

Cadence fidelity (#1336). A repetition step in a systemd calendar spec or a
cron field restarts at the *enclosing* field's boundary, so a step that does
not divide its field silently keeps the wrong cadence: ``*:0/45`` fires at :00
and :45 of every hour, never every 45 minutes. A step at or above the field's
own maximum is worse still — ``*:0/60`` and ``0/24:00:00`` are refused at parse
time ("Failed to parse calendar specification"), after which the timer unit
will not load at all. Every Tier-A cadence therefore goes through
:func:`_classify_cadence` before it is rendered: the cadences a calendar field
can keep faithfully get an ``OnCalendar=`` timer (keeping ``Persistent=true``
catch-up), and the rest get a monotonic systemd timer plus, for cron, an
explicit refusal. launchd is unaffected: ``StartInterval`` is exact seconds.
"""

from __future__ import annotations

import html
from dataclasses import dataclass
from enum import Enum, auto
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

# The scheduled command. ``creek`` is expected on PATH (or the operator edits
# the absolute path into the emitted unit — noted in docs/sync-scheduling.md).
_CREEK = "creek"

_MINUTES_PER_HOUR = 60
_HOURS_PER_DAY = 24
_MINUTES_PER_DAY = _MINUTES_PER_HOUR * _HOURS_PER_DAY

# The only steps a minute/hour field can repeat *faithfully*: a step must
# divide its field, or the counter's restart at the field boundary shortens one
# interval per cycle. Both ranges stop one short of the field maximum because a
# step at or above it is a parse error, not a slow cadence.
_FAITHFUL_MINUTE_STEPS = frozenset(
    step for step in range(1, _MINUTES_PER_HOUR) if _MINUTES_PER_HOUR % step == 0
)
_FAITHFUL_HOUR_STEPS = frozenset(
    step for step in range(1, _HOURS_PER_DAY) if _HOURS_PER_DAY % step == 0
)

# A daily cadence repeats once per day. It is spelled as a literal midnight
# time rather than a 24-hour step, because ``0/24`` exceeds the hour field's
# maximum and is refused — the very defect this module guards against.
_DAILY_STEP_DAYS = 1


class _CadenceKind(Enum):
    """How faithfully a scheduler's calendar grammar can express a cadence."""

    SUB_HOURLY = auto()
    """Repeats within the hour, on a step that divides 60."""

    HOURLY = auto()
    """Repeats on a whole-hour step that divides 24."""

    DAILY = auto()
    """Repeats once a day, at a literal time."""

    IRREGULAR = auto()
    """No faithful calendar expression exists at all."""


@dataclass(frozen=True)
class _Cadence:
    """A classified Tier-A cadence.

    Attributes:
        kind: Which calendar form, if any, can express the cadence exactly.
        step: The repetition step in the unit *kind* implies — minutes for
            ``SUB_HOURLY`` and ``IRREGULAR``, hours for ``HOURLY``, days for
            ``DAILY`` (always 1, and unused, since the daily form is literal).
    """

    kind: _CadenceKind
    step: int


def _classify_cadence(minutes: int) -> _Cadence:
    """Classify a Tier-A interval by the calendar form that can keep it.

    The branches are ordered sub-hourly, hourly, daily so that the hour step
    is tested *before* the daily special case: 24 divides 24, so a cadence of
    1440 minutes would otherwise be rendered as ``0/24:00:00``, which systemd
    refuses to parse. Excluding 24 from :data:`_FAITHFUL_HOUR_STEPS` is what
    routes it to :attr:`_CadenceKind.DAILY` instead.

    The daily test is an equality, never ``>=``: 2880 minutes is a legal
    configuration and must fall through to :attr:`_CadenceKind.IRREGULAR`
    rather than be quietly halved to one run a day.

    Args:
        minutes: The configured Tier-A interval, in minutes.

    Returns:
        The cadence, classified.
    """
    if minutes in _FAITHFUL_MINUTE_STEPS:
        return _Cadence(_CadenceKind.SUB_HOURLY, minutes)
    hours, leftover_minutes = divmod(minutes, _MINUTES_PER_HOUR)
    if leftover_minutes == 0 and hours in _FAITHFUL_HOUR_STEPS:
        return _Cadence(_CadenceKind.HOURLY, hours)
    if minutes == _MINUTES_PER_DAY:
        return _Cadence(_CadenceKind.DAILY, _DAILY_STEP_DAYS)
    return _Cadence(_CadenceKind.IRREGULAR, minutes)


# The one correct rendering per expressible kind, keyed by kind. IRREGULAR is
# absent from both tables *by construction*: that absence is what makes the
# renderers total without either of them re-deriving the divisibility rule.
_CALENDAR_SPEC_TEMPLATES: dict[_CadenceKind, str] = {
    _CadenceKind.SUB_HOURLY: "*:0/{step}",
    _CadenceKind.HOURLY: "*-*-* 0/{step}:00:00",
    _CadenceKind.DAILY: "*-*-* 00:00:00",
}

_CRON_SCHEDULE_TEMPLATES: dict[_CadenceKind, str] = {
    _CadenceKind.SUB_HOURLY: "*/{step} * * * *",
    _CadenceKind.HOURLY: "0 */{step} * * *",
    _CadenceKind.DAILY: "0 0 * * *",
}

# Every cadence in a day that a crontab can state exactly, ascending. Derived
# from the classifier rather than transcribed, so the refusal message can never
# recommend a cadence the renderer would itself turn down.
_CRON_EXPRESSIBLE_MINUTES: tuple[int, ...] = tuple(
    candidate
    for candidate in range(1, _MINUTES_PER_DAY + 1)
    if _classify_cadence(candidate).kind in _CRON_SCHEDULE_TEMPLATES
)


def _nearest_cron_cadences(minutes: int) -> tuple[int, ...]:
    """Return the cron-expressible cadences bracketing *minutes*.

    Args:
        minutes: A cadence cron cannot express.

    Returns:
        The closest expressible cadence below *minutes* and the closest above,
        in that order; one of them is missing when *minutes* falls outside the
        expressible range (nothing is longer than a day).
    """
    below = [value for value in _CRON_EXPRESSIBLE_MINUTES if value < minutes]
    above = [value for value in _CRON_EXPRESSIBLE_MINUTES if value > minutes]
    return (*below[-1:], *above[:1])


class UnrepresentableCadenceError(ValueError):
    """Raised when no crontab line states a Tier-A cadence exactly.

    Refusing beats guessing. A step that does not divide its field (``*/45``)
    is accepted by vixie cron and then keeps the wrong cadence, while a step
    above the field maximum (``*/90``) makes cron reject the *entire* crontab
    — so one wrong Tier-A line also costs the operator their correct Tier-B
    line. systemd has a monotonic fallback for these cadences; cron has none.

    Attributes:
        minutes: The rejected Tier-A cadence.
        alternatives: Cron-expressible cadences bracketing it, for a caller
            that wants to offer them in its own words rather than reprint the
            message.
    """

    def __init__(self, minutes: int) -> None:
        """Build the refusal, naming the cadence and a usable alternative.

        The message is composed here rather than at the ``raise`` site so the
        text lives with the type that owns it (tryceratops TRY003).

        Args:
            minutes: The Tier-A cadence cron cannot express.
        """
        self.minutes = minutes
        self.alternatives = _nearest_cron_cadences(minutes)
        alternatives = " or ".join(str(value) for value in self.alternatives)
        message = (
            f"cron cannot express a {self.minutes}-minute Tier-A cadence "
            "exactly: a step that does not divide its field restarts at the "
            "boundary, and a step above the field maximum makes vixie reject "
            "the whole crontab, taking the Tier B line with it. Nearest "
            f"cron-expressible cadences: {alternatives} minutes."
        )
        super().__init__(message)


def _require_positive_cadence(minutes: int) -> None:
    """Reject an interval that is not a cadence at all.

    ``SyncConfig`` already bounds the field at ``ge=1``, but these renderers
    are public API, so the guard lives with them rather than only at the one
    caller. It is not defensive padding: on systemd 257 a negative value draws
    "Failed to parse timer value, ignoring", which strips the timer's only
    trigger and gets the unit refused — the exact failure this module exists
    to prevent — while ``0min`` loads and re-fires with no delay.

    Args:
        minutes: The configured Tier-A interval, in minutes.

    Raises:
        ValueError: If *minutes* is less than one.
    """
    if minutes < 1:
        message = (
            f"tier_a_minutes must be at least 1 minute, got {minutes}. "
            "A zero or negative interval has no schedule to render: systemd "
            "refuses a negative timer value and spins on a zero one."
        )
        raise ValueError(message)


def _tier_args(vault: Path, tier: str) -> list[str]:
    """Return the ``creek sync`` argv for *tier* (path kept as one token)."""
    return [_CREEK, "sync", "--tier", tier, "--vault", str(vault)]


def _tier_command_quoted(vault: Path, tier: str) -> str:
    """Return the ``creek sync`` command line with the vault path quoted."""
    return f'{_CREEK} sync --tier {tier} --vault "{vault}"'


@dataclass(frozen=True)
class LaunchdPlists:
    """Rendered launchd plist contents for both tiers (macOS)."""

    tier_a: str
    tier_b: str
    tier_a_filename: str = "com.creek.sync.tier-a.plist"
    tier_b_filename: str = "com.creek.sync.tier-b.plist"


@dataclass(frozen=True)
class SystemdUnits:
    """Rendered systemd service+timer contents for both tiers (Linux)."""

    tier_a_service: str
    tier_a_timer: str
    tier_b_service: str
    tier_b_timer: str
    tier_a_service_filename: str = "creek-sync-tier-a.service"
    tier_a_timer_filename: str = "creek-sync-tier-a.timer"
    tier_b_service_filename: str = "creek-sync-tier-b.service"
    tier_b_timer_filename: str = "creek-sync-tier-b.timer"


def _plist(label: str, args: list[str], schedule_xml: str) -> str:
    """Render one launchd plist with *schedule_xml* as the cadence element.

    Each argv token becomes one ``<string>`` element (so a vault path with
    spaces stays intact), XML-escaped to stay well-formed for any path.
    """
    program_args = "".join(
        f"        <string>{html.escape(arg)}</string>\n" for arg in args
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
    """Render the Tier-A and Tier-B launchd plists for *vault*.

    Tier A uses ``StartInterval`` (the configured minutes, in seconds); Tier B
    uses ``StartCalendarInterval`` at the configured nightly hour.

    ``StartInterval`` is an exact number of seconds with no field boundaries to
    restart at, so launchd expresses *every* cadence faithfully — including the
    ones systemd and cron cannot. No classification is needed here.

    Raises:
        ValueError: If *tier_a_minutes* is less than one.
    """
    _require_positive_cadence(tier_a_minutes)
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
            _tier_args(vault, "A"),
            tier_a_schedule,
        ),
        tier_b=_plist(
            "com.creek.sync.tier-b",
            _tier_args(vault, "B"),
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


def _timer(description: str, directives: str, unit: str) -> str:
    """Render a systemd timer firing *unit* on the given schedule.

    Args:
        description: The unit's ``Description=``.
        directives: The newline-terminated cadence block for the ``[Timer]``
            section — a calendar or a monotonic schedule, as produced by
            :func:`_calendar_directives` or :func:`_monotonic_directives`.
        unit: The service unit the timer activates.

    Returns:
        The complete timer unit file.
    """
    return (
        "[Unit]\n"
        f"Description={description}\n\n"
        "[Timer]\n"
        f"{directives}"
        f"Unit={unit}\n\n"
        "[Install]\n"
        "WantedBy=timers.target\n"
    )


def _calendar_directives(spec: str) -> str:
    """Return the ``[Timer]`` cadence block for a wall-clock schedule.

    Args:
        spec: An ``OnCalendar=`` value systemd parses and keeps faithfully.

    Returns:
        The directive lines, newline-terminated. ``Persistent=true`` is safe to
        promise here: a tick missed while the host slept runs on next boot.
    """
    return f"OnCalendar={spec}\nPersistent=true\n"


def _monotonic_directives(minutes: int) -> str:
    """Return the ``[Timer]`` cadence block for an inexpressible cadence.

    A monotonic timer counts *minutes* from boot and from its own last run, so
    it keeps the interval exactly where no calendar spec can. What it cannot do
    is catch up: ``Persistent=`` is inert without ``OnCalendar=``, so it is
    omitted rather than emitted as a promise the unit cannot keep, and the
    emitted comment tells the operator what that costs.

    Args:
        minutes: The configured Tier-A cadence, in minutes.

    Returns:
        The directive lines, newline-terminated, led by the disclosure comment.
    """
    return (
        "# No systemd calendar expression keeps a cadence of "
        f"{minutes} minutes, so this\n"
        f"# timer is monotonic: it counts {minutes} minutes from boot and from "
        "its own\n"
        "# last run. Ticks missed while the host was off get no catch-up — "
        "that needs\n"
        "# an OnCalendar= schedule, which cannot state this cadence.\n"
        f"OnBootSec={minutes}min\n"
        f"OnUnitActiveSec={minutes}min\n"
    )


def _tier_a_timer_directives(minutes: int) -> str:
    """Return the ``[Timer]`` cadence block for a Tier-A interval.

    Args:
        minutes: The configured Tier-A interval, in minutes.

    Returns:
        A calendar block when the cadence has a faithful calendar spec, and a
        monotonic block otherwise. Total: every positive interval renders.
    """
    cadence = _classify_cadence(minutes)
    template = _CALENDAR_SPEC_TEMPLATES.get(cadence.kind)
    if template is None:
        return _monotonic_directives(minutes)
    return _calendar_directives(template.format(step=cadence.step))


def render_systemd_units(
    *,
    vault: Path,
    tier_a_minutes: int = 30,
    tier_b_hour: int = 3,
) -> SystemdUnits:
    """Render the Tier-A/B systemd service + timer units for *vault*.

    Tier A fires every ``tier_a_minutes``, on whichever timer form can keep
    that cadence honestly (see :func:`_tier_a_timer_directives`). Tier B fires
    nightly at ``tier_b_hour`` (``OnCalendar=*-*-* HH:00:00``) — a literal time
    with no step, so it is faithful at every hour and is left untouched.
    ``Persistent`` means a missed tick (host asleep) runs on next boot.

    Raises:
        ValueError: If *tier_a_minutes* is less than one.
    """
    _require_positive_cadence(tier_a_minutes)
    return SystemdUnits(
        tier_a_service=_service(
            "Creek sync (Tier A: pull/ingest/rules-classify)",
            _tier_command_quoted(vault, "A"),
        ),
        tier_a_timer=_timer(
            "Creek sync Tier A schedule",
            _tier_a_timer_directives(tier_a_minutes),
            "creek-sync-tier-a.service",
        ),
        tier_b_service=_service(
            "Creek sync (Tier B: llm-classify/link/index)",
            _tier_command_quoted(vault, "B"),
        ),
        tier_b_timer=_timer(
            "Creek sync Tier B schedule",
            _calendar_directives(f"*-*-* {tier_b_hour:02d}:00:00"),
            "creek-sync-tier-b.service",
        ),
    )


def _tier_a_cron_schedule(minutes: int) -> str:
    """Return the five cron schedule fields for a Tier-A interval.

    Args:
        minutes: The configured Tier-A interval, in minutes.

    Returns:
        The schedule, e.g. ``*/30 * * * *``.

    Raises:
        UnrepresentableCadenceError: If no crontab line states *minutes*
            exactly. cron, unlike systemd, has no monotonic fallback.
    """
    cadence = _classify_cadence(minutes)
    template = _CRON_SCHEDULE_TEMPLATES.get(cadence.kind)
    if template is None:
        raise UnrepresentableCadenceError(minutes)
    return template.format(step=cadence.step)


def render_crontab(
    *,
    vault: Path,
    tier_a_minutes: int = 30,
    tier_b_hour: int = 3,
) -> str:
    """Render a two-line crontab for the Tier-A/B sync passes.

    Args:
        vault: The vault both tiers operate on.
        tier_a_minutes: How often the Tier-A pass runs, in minutes.
        tier_b_hour: The hour (0-23) the nightly Tier-B pass runs at.

    Returns:
        The crontab text, comment header included.

    Raises:
        ValueError: If *tier_a_minutes* is less than one.
        UnrepresentableCadenceError: If ``tier_a_minutes`` has no exact cron
            expression. Nothing is returned in that case — a wrong Tier-A step
            would make vixie reject the whole file, Tier B included.
    """
    _require_positive_cadence(tier_a_minutes)
    tier_a_schedule = _tier_a_cron_schedule(tier_a_minutes)
    tier_a = f"{tier_a_schedule} {_tier_command_quoted(vault, 'A')}"
    tier_b = f"0 {tier_b_hour} * * * {_tier_command_quoted(vault, 'B')}"
    return (
        "# Creek sync schedule (crontab) — install with `crontab -e`\n"
        f"{tier_a}\n"
        f"{tier_b}\n"
    )
