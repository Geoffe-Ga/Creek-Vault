# Tracer phase — automated issue inflow is off

**Status:** active as of 2026-08-17. Owner: Geoff.

Every scheduled source that filed issues into this repository has had its
`schedule:` trigger commented out. New work now enters the backlog exactly one
way: a human or an autonomous agent finds a real flaw while running a tracer
bullet, and files it.

This is coordinated with the same shutoff in `Geoffe-Ga/adepthood` — see
`docs/tracer-phase.md` there for the full rationale. The short version: the
end-to-end path between adepthood and this vault has never been run once, and
the backlog kept growing with work that did not serve proving it. Scans stand
down until the vertical slice is proven.

## Creek's part in the tracer bullets

Two things in this repository gate the adepthood-side runs:

1. **`/v1` must serve the four ratified capabilities** so a journal save
   replicates and a reflection returns grounded. This is the first tracer
   bullet, and it is the close condition of adepthood#2043.
2. **`/v1` publishes no upload route** (#1524). adepthood PR #2172 established
   that `UPLOAD`, `SAVE` and `CLASSIFY` are permanently unavailable at contract
   0.3.0 — the capability vocabulary is a closed enum under
   `additionalProperties: false`, so no conformant vault can advertise them.
   Until #1524 lands, a user cannot seed a vault over the network at all, and
   the adepthood-side seeding issues (#2250–#2255) are blocked on it rather than
   on anything in adepthood.

## What was turned off

Each workflow keeps `workflow_dispatch:`, so any can still be fired by hand.

`scan-bugs`, `scan-security`, `scan-deps`, `scan-dead-code`, `scan-complexity`,
`scan-coverage`, `scan-perf`, `scan-todo`, `scan-docs`, `scan-types`,
`scan-mutation` (producers); `deslop` (producer matrix); `hopper` (hourly
dispatcher that refilled the queue off-schedule); `scan-groom` (consumer);
`dependabot-to-ralph-issue` (bridge — both the weekly backfill and the real-time
`pull_request_target` path).

**Left running:** `graph-update` (weekly) — graph maintenance, files no issues.

**Dependabot** is untouched and still opens PRs, so security advisories stay
visible. Only the bridge that minted one backlog issue per PR is off.

## Re-enabling

The crons are preserved verbatim behind `#` with a `[tracer-phase]` marker:

```bash
grep -rn "\[tracer-phase\]" .github/workflows/
```

Scheduled workflows run from the **default branch**, so a cron change takes
effect only once merged to `main`. Disable them in the Actions UI if you need
the shutoff to take effect sooner.

## The priority scheme while this is in force

- **P0** — on the tracer path, carrying the `tracer` label (#1523, #1524,
  #1526), plus four tier-safety guardrails (#1031, #1106, #1212, #1529) held at
  P0 because the tracer runs are what push real journal content across the
  boundary they protect.
- **P1** — has an open PR; finish what is in flight. Currently empty.
- **P2** — real, hand-written work that is neither on the path nor in flight.
- **P3** — parked: scan-generated findings, deferred epics, and anything blocked
  on an upstream decision.

All 323 open issues now carry exactly one tier (7 / 0 / 149 / 167). The legacy
`priority-high` / `priority-medium` / `priority-low` labels are retired.

The `scan:*`-generated P3 issues are deliberately left open and untouched. They
stop growing now that the scans are off, they do not compete with P0/P1 in
`pick-next.sh`, and closing them would discard real machine-found findings. The
P2/P3 boundary is not reached during this phase; it is kept because it carries
real signal about relative value.

## Known overlap, deliberately not resolved

Epics #1041 ("six config surfaces ship to every vault and are read by nothing")
and #1316 ("six `creek/clean` modules are unreachable … the whole `cleaning.*`
config tree is dead") cover adjacent ground and may be the same work seen from
two angles. They were **not** merged or closed: #1316 is the parent of the live
decomposition #1517–#1520, and the two "sixes" are different sets. This needs an
owner decision, not an automated one.
