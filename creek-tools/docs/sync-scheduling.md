# Scheduling `creek sync`

`creek sync` runs the two-tier incremental pipeline (see
[idempotent-ingest.md](idempotent-ingest.md)). To keep your vault current
without running it by hand, schedule it on a host. Creek **emits** the schedule
units for you; activating them is a one-time manual step (Creek never runs
`launchctl`/`systemctl`/`crontab` for you).

```bash
creek sync --install-schedule launchd --vault /path/to/vault   # macOS laptop
creek sync --install-schedule systemd --vault /path/to/vault   # Linux always-on box
```

Both emit a **Tier A** unit (every 30 min by default: pull → incremental ingest
→ rules-classify) and a **Tier B** unit (nightly ~03:00: LLM-classify → link →
index). The cadence comes from your config (`sync.tier_a_interval_minutes`,
`sync.tier_b_hour`), so edit those before installing if you want a different
rhythm. Pass `--schedule-out-dir <dir>` to write the units somewhere other than
the host-standard location.

## Which host?

Decision #4 of the ingest SPEC is **build both** — pick per how you work:

| | **Laptop (launchd)** | **Always-on mini / VPS (systemd or cron)** |
|---|---|---|
| Runs when | only while the laptop is awake and logged in | 24/7 |
| Missed ticks | skipped while asleep (launchd coalesces) | `Persistent=true` runs a missed tick on next boot — but only for cadences that get a wall-clock timer (see below) |
| Best for | keeping a personal machine's journal current | Discord live-capture and anything that must not miss windows |
| Setup | `launchctl load ~/Library/LaunchAgents/com.creek.sync.tier-*.plist` | `systemctl --user daemon-reload && systemctl --user enable --now creek-sync-tier-*.timer` (or `crontab -e`) |

The always-on box is recommended if you want the vault genuinely current
around the clock — a closed laptop simply doesn't run Tier A.

## Supported cadences

`sync.tier_a_interval_minutes` accepts any integer >= 1 with no upper bound,
but systemd and cron can only *state* some of those values exactly — a
repetition step that doesn't divide its field restarts at the field boundary
and quietly drifts (`*:0/45` fires at :00 and :45 of every hour: 45 minutes,
then 15, never every 45). Which timer form you get depends on the cadence:

| cadence | systemd | cron | launchd |
|---|---|---|---|
| divides 60 (1, 2, 3, 4, 5, 6, 10, 12, 15, 20, 30) | wall-clock `OnCalendar=*:0/N`, catches up | `*/N * * * *` | exact (`StartInterval`) |
| whole hours dividing 24 (60, 120, 180, 240, 360, 480, 720) | wall-clock `OnCalendar=*-*-* 0/H:00:00`, catches up | `0 */H * * *` | exact (`StartInterval`) |
| daily (1440) | wall-clock `OnCalendar=*-*-* 00:00:00`, catches up | `0 0 * * *` | exact (`StartInterval`) |
| anything else (e.g. 45, 90, 300) | monotonic timer (`OnBootSec=`/`OnUnitActiveSec=`), no catch-up | refused — see below | exact (`StartInterval`) |

Those 19 cadences are the only ones a calendar minute/hour field can keep
faithfully, on either scheduler. Outside that set, systemd falls back to a
monotonic timer that keeps the interval exactly but can't catch up a missed
tick (`Persistent=` is inert without `OnCalendar=`, so the emitted `.timer`
omits it and says so in a comment); a monotonic timer may also fire once
immediately on activation if the host has been up longer than the interval.
cron has no such fallback — and a bad step doesn't just corrupt the Tier-A
line, vixie refuses the *whole* crontab, taking the Tier-B line with it — so
for these cadences Creek prints an explanation and the nearest expressible
alternatives instead of a crontab line. No config value is ever rejected;
only the cron *hint* is unavailable for it.

launchd's `StartInterval` counts exact seconds with no field to restart at,
so it is exact at every cadence, including all the ones above.

## Where tokens live

OAuth credentials/tokens (e.g. Google Drive `credentials.json` / `token.json`)
and any API keys/consent are read from **the environment of the host that runs
the adapter** — never from the emitted unit files, which contain only paths and
schedules. So:

- Install the schedule on the host where the tokens already live (or copy the
  token files there first), and ensure that host's user environment has the
  required keys/consent set (e.g. in the shell profile the scheduler inherits).
- Moving the schedule to a different host means moving (or re-authorising) the
  tokens on that host too.

## Activating the emitted units

**launchd (macOS):**

```bash
launchctl load ~/Library/LaunchAgents/com.creek.sync.tier-a.plist
launchctl load ~/Library/LaunchAgents/com.creek.sync.tier-b.plist
```

**systemd (Linux, user units):**

```bash
systemctl --user daemon-reload
systemctl --user enable --now creek-sync-tier-a.timer creek-sync-tier-b.timer
```

`daemon-reload` is required, not optional: systemd caches a unit it
previously refused to load, so skipping it can make a genuine fix look like
it "didn't work."

**cron (any POSIX host):** the systemd install also prints equivalent crontab
lines — paste them into `crontab -e`.

> The emitted units invoke `creek` from `PATH`. If `creek` is not on the
> scheduler's `PATH` (e.g. it lives in a `uv`/virtualenv), edit the unit to use
> the absolute path to the `creek` binary before activating.

> Vault paths containing spaces (e.g. `~/Documents/My Notes`) are handled
> correctly: launchd passes the path as a single argument, and the systemd/cron
> commands quote it.

## Fixing an already-installed broken timer

If you ran `--install-schedule systemd` before this fix with a
`sync.tier_a_interval_minutes` that did not divide 60, the old renderer wrote
`OnCalendar=*:0/N` regardless. That failed in **two different ways**, and
which one you hit decides how you recover. Check first:

```bash
grep OnCalendar ~/.config/systemd/user/creek-sync-tier-a.timer
systemctl --user list-timers 'creek-sync-*'
```

- **N >= 60 (e.g. `60`, `90`, `1440`) — the loud failure.** The spec
  (`*:0/60`) is not parseable, so systemd discarded it, found the timer had
  no trigger, and refused the unit: `enable --now` failed and **Tier A never
  ran**. `list-timers` shows no entry. There is no running trigger and no
  double-fire risk.
- **N < 60 but not a divisor of 60 (`25`, `40`, `45`, `50`, `59`) — the quiet
  failure.** `*:0/45` *is* parseable, so the timer loaded, enabled and has
  been **running at the wrong cadence** ever since: it fires at :00 and :45
  of each hour — 45 minutes apart, then 15 — not every 45 minutes.
  `list-timers` shows an entry, which is why nothing looked wrong.

Recovery is the same re-install in both cases. Re-run `creek sync
--install-schedule systemd --vault /path/to/vault`: the four unit filenames
are unchanged, so the fixed content overwrites the broken content in place —
no stale unit lingers and there is nothing to delete. Then:

```bash
systemctl --user daemon-reload
systemctl --user enable --now creek-sync-tier-a.timer creek-sync-tier-b.timer
systemctl --user restart creek-sync-tier-a.timer
```

`enable --now` is safe to repeat and covers the refused case. The `restart` is
belt-and-braces for the quiet case, where the timer is already active: it
forces the unit to be re-armed from the new file rather than relying on the
reload to do it. Both commands are safe to run when they are not needed.
Confirm with `systemctl --user list-timers 'creek-sync-*'` — Tier A should
show a NEXT/LEFT value consistent with your configured interval.

For cron: if you pasted a `*/60`-style line, vixie rejected the whole crontab
and installed nothing, so there is nothing to clean up. If you pasted a
`*/45`-style line it was accepted and has been misfiring — edit it out with
`crontab -e`. Re-running the install now prints an expressible line (e.g.
`0 */1 * * *` for a 60-minute interval), or explains why that cadence has no
crontab equivalent.

Beyond that one `crontab -e` edit, don't delete or disable anything yourself
— no `rm`, no `systemctl disable`, no `reset-failed`. Creek never runs
`systemctl`/`crontab` on your behalf (see the top of this doc).
