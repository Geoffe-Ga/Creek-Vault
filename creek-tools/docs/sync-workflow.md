# The `creek sync` workflow

`creek sync` keeps your vault current by running the ingest pipeline on a
schedule, in two tiers. It builds on idempotent ingest
([idempotent-ingest.md](idempotent-ingest.md)) and is scheduled with the deploy
adapters ([sync-scheduling.md](sync-scheduling.md)).

## The two tiers

| | **Tier A** (cheap, frequent) | **Tier B** (expensive, nightly) |
|---|---|---|
| Default cadence | every 30 min | ~03:00 |
| Per source | pull → incremental ingest → rules-classify | — |
| Global | — | LLM-classify → link → index |
| Cost | offline, O(changed) | LLM + O(n²) linking + index rebuild |

**Why two tiers (R6):** linking is O(n²) and rebuilds the whole resonance graph,
so it must not run on every 30-minute tick. Tier A stays cheap (no link/index);
Tier B does the heavy global passes once a day.

```bash
creek sync --tier A --vault /path/to/vault    # cheap pass (what the frequent timer runs)
creek sync --tier B --vault /path/to/vault    # nightly global pass
creek sync --tier A --dry-run --vault ...      # echo the plan without running
```

## A bare invocation executes — `--dry-run` is opt-in

There is no preview mode by default. `creek sync --tier B` really runs: **one
LLM call per unclassified fragment** across the whole vault, then the link pass,
then a full index rebuild. On a cloud `classification` provider that is real
token spend and real data egress. Pass `--dry-run` to see the ordered plan
without any of it.

Before a real Tier-B pass, `creek sync` prints what it is about to do —
including how many fragments it may bill for — and then:

| where it runs | what happens |
|---|---|
| Interactive terminal (stdin is a TTY) | asks `Continue?`; answering no aborts before any step runs and records nothing |
| Scheduled or piped (launchd, systemd, cron, CI, any non-TTY) | **proceeds unprompted**, and says so in the output |

The scheduled case is deliberate: the units written by `--install-schedule` are
bare `creek sync --tier B` invocations with no stdin, so a prompt there would
block a timer nobody is watching. It does mean the confirmation protects the
operator at a keyboard and **not** the nightly run — the cost line is printed
either way precisely so the scheduler log records what the pass cost. Use
`--yes` to skip the prompt in an interactive shell.

Tier A is not gated: it is the cheap, offline pass.

## Enabling sources

Each source has an on/off toggle under `sync.sources` in your config. The
journal and Google Drive are on by default; the rest are off until you opt in:

```yaml
sync:
  tier_a_interval_minutes: 30
  tier_b_hour: 3
  sources:
    journal: true
    gdrive: true
    discord: false
```

`creek sync --tier A` runs only the enabled sources. `--source <name>` runs a
single source on demand (and **overrides** its toggle — handy for a one-off).

## Self-healing

Every step is idempotent — incremental ingest skips unchanged units, edited
fragments update in place, and OPS-001 short-circuits already-classified
fragments. So a **missed tick needs no catch-up queue**: the next run simply
re-derives what changed. A laptop that was asleep, or a one-off manual run,
both converge to the same correct vault.

## Reading status

`creek sync --status` shows what each source last did:

```
$ creek sync --status --vault /path/to/vault
Last sync: tier=A at 2026-06-26T08:30:00+00:00
┏━━━━━━━━━┳━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━┓
┃ source  ┃ last tier ┃ last run                  ┃ ingested ┃
┡━━━━━━━━━╇━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━┩
│ journal │ A         │ 2026-06-26T08:30:00+00:00 │ 3        │
│ gdrive  │ A         │ 2026-06-26T08:30:00+00:00 │ 0        │
└─────────┴───────────┴───────────────────────────┴──────────┘
```

The state lives in `00-Creek-Meta/State/sync/last-run.json`. Each tick also
emits structured `creek.cli` log lines (`sync.tier_a.ingest source=… ingested=…`,
`sync.tier_a.done`, `sync.tier_b.done`) for log-based monitoring.

## Scheduling it

See [sync-scheduling.md](sync-scheduling.md) for emitting and activating the
launchd / systemd / cron units, and for the laptop-vs-always-on trade-off and
where OAuth tokens live.
