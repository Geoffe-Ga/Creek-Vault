"""Scheduling adapters for ``creek sync`` (#679).

Emits launchd / systemd / cron artifacts that run the two-tier sync on a
schedule. These are *config artifacts* the operator installs manually — Creek
never shells out to ``launchctl``/``systemctl``/``crontab``.

Not every cadence survives every host's grammar (#1336): cron refuses one it
cannot state exactly, raising :class:`UnrepresentableCadenceError`, while
systemd falls back to a monotonic timer and launchd is exact throughout.
"""

from creek.sync.schedule import (
    LaunchdPlists,
    SystemdUnits,
    UnrepresentableCadenceError,
    render_crontab,
    render_launchd_plists,
    render_systemd_units,
)

__all__ = [
    "LaunchdPlists",
    "SystemdUnits",
    "UnrepresentableCadenceError",
    "render_crontab",
    "render_launchd_plists",
    "render_systemd_units",
]
