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
| Missed ticks | skipped while asleep (launchd coalesces) | `Persistent=true` runs a missed tick on next boot |
| Best for | keeping a personal machine's journal current | Discord live-capture and anything that must not miss windows |
| Setup | `launchctl load ~/Library/LaunchAgents/com.creek.sync.tier-*.plist` | `systemctl --user enable --now creek-sync-tier-*.timer` (or `crontab -e`) |

The always-on box is recommended if you want the vault genuinely current
around the clock — a closed laptop simply doesn't run Tier A.

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
systemctl --user enable --now creek-sync-tier-a.timer creek-sync-tier-b.timer
```

**cron (any POSIX host):** the systemd install also prints equivalent crontab
lines — paste them into `crontab -e`.

> The emitted units invoke `creek` from `PATH`. If `creek` is not on the
> scheduler's `PATH` (e.g. it lives in a `uv`/virtualenv), edit the unit to use
> the absolute path to the `creek` binary before activating.
