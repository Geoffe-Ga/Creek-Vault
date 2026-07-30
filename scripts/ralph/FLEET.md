# Ralph Fleet — worktree-parallel Ralph

Ralph's outer loop can work **up to `max_workers` (default 4) parallelizable
backlog issues at once**, each in its own git worktree, and still preserve every
correctness guarantee of the sequential loop. This document is the design; the
mechanism is `scripts/ralph/fleet.sh`, the orchestration lives in
`.claude/commands/ralph-tick.md`, and the per-issue worker contract lives in
`scripts/ralph/PROMPT.md` (run by the `ralph-worker` agent).

## The core principle: optimistic parallelism, pessimistic merge

Two issues are "parallelizable" only as a **speculation** — we cannot perfectly
predict which files a change will touch before we make it. So the loop never
*relies* on that speculation for correctness. Instead:

- **Pick optimistically.** `pick-next.sh` hands out issues that *look*
  independent (different epics, not marked `solo`), up to the worker cap.
- **Work in isolation.** Each issue gets its own worktree under
  `.ralph/worktrees/issue-<N>` on branch `issue/<N>-<slug>`, so concurrent edits
  never collide on disk. Each worktree runs the full four-gate pipeline exactly
  as the sequential loop does.
- **Merge pessimistically, but never with a barrier.** Merges to `main` are
  **serialized** (one at a time — the single orchestrator session serializes them
  for free) and each merge is **always up-to-date**: a lane merges only when
  `scripts/ralph/pr-ready.sh` prints `ready` — `LGTM` + CI-green +
  `mergeStateStatus CLEAN` + the compare API's `behind_by == 0`.
  **`CLEAN` is not freshness.** GitHub computes `mergeStateStatus BEHIND` only
  when the base branch enforces strict/up-to-date status checks, which this
  repo does not — so `CLEAN` means only "no merge conflict" and routinely
  reports on a PR that is dozens of commits stale. Measured live: PR #943
  reported `MERGEABLE`/`UNSTABLE` while the compare API said `behind_by: 22`;
  PR #863 said `44`. `behind_by` is the only signal that actually answers "up
  to date with `main`", so `pr-ready.sh` checks it — **lazily**, only for a
  lane that would otherwise print `ready`, so the fleet never sync-thrashes
  probing every lane on every wake. If a sibling merged after this lane went
  green, the lane reads `behind` (`mergeStateStatus BEHIND`, or `CLEAN` with
  `behind_by > 0`); it **syncs the new `main` into its branch (by merge, not
  rebase — a plain push updates the PR and re-runs CI, never a force-push)**
  and merges on a later wake once green again. A lane that cannot cleanly sync
  **drops to Gate 1**. This sync is **lazy** — a lane only pays it when it is
  itself about to merge, not proactively every time any sibling merges.
- **Never wait on the slowest lane.** Whichever lane is ready merges immediately;
  the slot it frees refills at once. A fast lane at Gate 4 never waits for a slow
  lane at Gate 1.

The result: an imperfect independence guess costs at most a sync — it can
**never** merge broken or conflicting code (every merge is re-validated against
the real, updated `main`), and it **never** stalls a ready lane behind a slow one.

```
pick optimistically ──▶ N lanes build in parallel (isolated worktrees)
        │
   a lane goes LGTM+green ──▶ up-to-date (behind_by==0)? ──▶ merge NOW, refill its slot
        │                 ──▶ behind? ── sync main in (lazy) ── re-green ── merge next wake
   sync conflict?         ──▶ that lane drops to Gate 1 (never a forced merge)
```

## Why worktrees (not branches in one tree, not clones)

- **Branches in one working tree** serialize edits — you can only have one
  checked out at a time. That is the *sequential* loop.
- **Full clones** duplicate history and lose the shared object store and hooks.
- **Worktrees** share one `.git` (one object store, one set of hooks, one config)
  while giving each issue its own checked-out files and index. That is exactly
  "N isolated working copies of one repo" — the right primitive here.

Ralph manages its **own persistent** worktrees rather than the `Agent` tool's
ephemeral `isolation: "worktree"` because a worktree must **survive across wakes**:
Gates 3–4 (CI + review) span many wakes, with the turn ending in between.

## Execution model — an event-driven worker pool

One re-entrant orchestrator session (`/loop /ralph-tick`) is the single brain. It
runs a **worker pool**: up to `max_workers` **lanes**, each one issue in its own
worktree moving through the four gates **independently, on its own clock**. There
is **no per-tick barrier and no all-lanes Monitor** — the orchestrator is woken by
*per-lane events* and acts on whichever lane the wake is about.

On each wake it:

1. **Reconciles** — releases worktrees whose PR merged/closed (`fleet.sh
   reconcile`), freeing their slots.
2. **Merges every ready lane** — any PR `pr-ready.sh` classifies `ready` (`LGTM`
   + green + `mergeStateStatus CLEAN` + `behind_by == 0`) merges *now*,
   serialized; a `behind` lane lazily syncs first and merges on a later wake. A
   ready lane never waits for a slow one. `optout` lanes are skipped entirely.
3. **Advances failing lanes** — a `ralph-worker` is dispatched into the worktree
   of any PR that needs a fix (CI failure → `ci-debugging`; `CHANGES_REQUESTED` →
   `address-feedback`).
4. **Refills every open slot** — while `fleet.sh free > 0` and `pick-next.sh`
   yields a compatible issue, `assign` (or, for a `dependencies` issue already
   riding a bot PR, `adopt`) a worktree and launch a `ralph-worker`.
5. **Arms per-lane wakes** — background workers wake it on their own completion;
   each in-flight PR is `subscribe_pr_activity`-subscribed so its CI/verdict wakes
   it independently; a modest `ScheduleWakeup` backstops the CI-success /
   `behind→green` transitions the webhook doesn't deliver. **A lane going stale
   is invisible to webhooks entirely** — `main` moving emits no event on the
   lane's own PR — so this periodic fallback wake is the *only* thing that
   ever notices it. Then it ends the turn.

**Workers are background tasks.** Each `ralph-worker` is launched with
`run_in_background: true` and **never awaited** — launch, end the turn, and let its
completion be its own wake. Awaiting a batch of workers would re-introduce the
slowest-lane barrier this design exists to avoid. Workers never merge, never touch
`main`, and never coordinate with each other — all cross-lane coordination (merge
serialization, lazy sync, slot allocation) is the orchestrator's job: **fan-out
for building, serialize only the merge.**

## Which issues run in parallel (the safety gate)

`pick-next.sh` is parallel-aware. Beyond the existing require/exclude label
filters and open-PR exclusion, it:

- **Excludes live worktree issues** (started, PR not yet opened) so the same
  issue is never handed to two workers.
- Gives the **first** worker (empty fleet) the lowest eligible issue, exactly as
  before — sequential behavior is unchanged when nothing else is active.
- For **additional** workers, only returns an issue *independent* of every active
  one:
  - never an issue labeled **`solo`** (`RALPH_SOLO_LABEL`) while others are active,
    and once a `solo` issue is active it monopolizes the fleet;
  - unless labeled **`parallelizable`** (`RALPH_PARALLEL_LABEL`), never an issue
    that shares an **epic** label with an active issue (same epic ⇒ likely
    ordered/overlapping). Toggle with `RALPH_RESPECT_EPICS=0`.

These heuristics only reduce *sync churn*; they are **not** the correctness
mechanism. Correctness is the serialized, always-up-to-date merge (lazy sync +
re-green when `behind`) described above.

## Configuration (`scripts/ralph/state.json`)

| Key | Default | Meaning |
| --- | --- | --- |
| `max_workers` | `4` | Maximum concurrent worktrees. |
| `parallel_enabled` | `true` | `false` ⇒ effective cap of 1 (classic sequential Ralph, worktree-isolated). |

Set `parallel_enabled` to `false` (or `max_workers` to `1`) to fall straight
back to the one-issue-at-a-time loop with zero other changes.

## `fleet.sh` reference

| Command | Effect |
| --- | --- |
| `list` | `<issue>\t<branch>\t<path>` per active worktree. |
| `active` | Active issue numbers, space-separated. |
| `count` / `free` | Active count / remaining capacity (honors `parallel_enabled`). |
| `path <N>` | Worktree path for issue N (exit 1 if none). |
| `assign <N> <slug>` | Create/reuse a worktree off `origin/main`; prints its path; refuses when full. |
| `adopt <N> <PR>` | The bot-PR variant of `assign`: create/reuse a worktree for issue N attached to PR `<PR>`'s **existing** head branch (e.g. Dependabot's), so fixes push there instead of opening a second PR. Refuses a fork PR (its branch is not pushable), a local branch diverged from `origin/<ref>`, and a full fleet. |
| `sync <N>` | Merge latest `origin/main` into issue N's branch (no force-push); exit 3 on conflict (aborted, left clean). |
| `release <N>` | Remove issue N's worktree + delete its branch. |
| `reconcile` | Release worktrees whose PR merged/closed or whose issue is closed; prune. |

`.ralph/` is git-ignored. Worktree state is always **derived from live git +
GitHub**, never from stored bookkeeping, so the loop stays re-entrant.

## `pr-ready.sh` tokens (the merge gate)

`scripts/ralph/pr-ready.sh <PR_NUMBER>` is the single authoritative classifier
for a lane's mergeability; the orchestrator merges a lane **if and only if**
it prints `ready`. No other evidence — an eyeballed rollup, a `gh pr checks`
grep — is allowed to merge a lane. A non-zero exit (empty `$STATUS`) means the
helper itself hit a tooling error and could not classify the lane at all; that
includes an UNDETERMINABLE `optout` label/body lookup, which fails closed
rather than reading as "no hold."

| Token | Meaning |
| --- | --- |
| `ready` | `LGTM` fresh + CI green + `mergeStateStatus CLEAN` + `behind_by == 0`. Merge now. |
| `ready-unreviewed` | CI green (with a real non-review `SUCCESS`, not just skipped checks) + `CLEAN` + `behind_by == 0`, but no review gate can ever exist — Dependabot authored the PR and pushed HEAD, so every review-rollup entry is `SKIPPED`. Report it; do not merge without the repo owner's OK — see `ralph-tick.md` Step 1 for the full reasoning and what would have to change first. |
| `behind` | `LGTM` + green, but stale — `mergeStateStatus BEHIND`, or `CLEAN` with `behind_by > 0`. Sync (`fleet.sh sync`) and re-green. |
| `pending` | CI still running, or no checks registered yet. Wait. |
| `ci-failed` | A check failed or errored. Advance via `ci-debugging`. |
| `awaiting-review` | CI green but no fresh `LGTM` verdict yet (missing, stale, or non-LGTM). Wait, or check for a hidden merge conflict masquerading as a missing review (see `ralph-tick.md` Step 1). |
| `optout` | `do-not-auto-merge` on the PR's own labels, or on the labels of the last issue it closes. Leave the lane **entirely** alone — no merge, no sync, no dispatch; a lane it already occupies stays occupied. |

## Tests

Three offline suites cover the fleet, all run in CI by
`.github/workflows/ralph-fleet-tests.yml` on any `scripts/ralph/**` change —
which also runs `shellcheck --severity=warning scripts/ralph/*.sh` first.
(`creek-tools/scripts/lint-extended.sh` only shellchecks `scripts/*.sh`
relative to `creek-tools/`, so before that step these files were linted by the
pre-commit hook alone, i.e. not at all for anyone who bypassed it.)

- `scripts/ralph/test_fleet.sh` builds a throwaway repo (with an `origin` remote
  and a fake `gh`) and exercises assign / adopt / list / count / free / path /
  sync (clean **and** conflicting) / release / reconcile. It also runs those
  subcommands **from inside a linked worktree**, which is where a worker
  actually calls them — before the `repo_root()` fix the fleet read as empty
  from there, so `free` reported the full cap and the orchestrator would have
  started workers past `max_workers`. `adopt` coverage includes the fork
  refusal (including a `|` smuggled into the branch name, with a legal
  same-repo twin so the guard is not just "reject every `|`"), a local branch
  diverged from `origin/<ref>`, both malformed head-lookup shapes, the cap, and
  a tag that shadows the branch name.
- `scripts/ralph/test_pick_next.sh` stubs `gh` and exercises the picker's
  parallel-awareness: first-worker-lowest, worktree exclusion, in-flight-PR
  exclusion, the `solo` guard (candidate and active), the same-epic guard, the
  `parallelizable` override, and `RALPH_RESPECT_EPICS=0`.
- `scripts/ralph/test_pr_ready.sh` stubs `gh` and exercises the merge-readiness
  classifier: CI exit-code mapping, `mergeStateStatus`, the stale-verdict guard
  (an LGTM comment posted before the current HEAD push does not count), the
  `behind_by` freshness guard and every one of its fail-closed answers, the
  `optout` hold on both the PR and its linked issue (including that an
  *undeterminable* hold exits 2 rather than reading as "no hold", and that the
  **last** `Closes #N` wins over an upstream changelog's `Fixes #N`), the full
  `ready-unreviewed` matrix, and — via sentinel files — that both new probes
  stay **lazy**, so a pending/red/unreviewed/held lane never pays for them.
  Two assertions are cross-file: they pin that
  `.github/workflows/code-review.yml` still declares the `claude-review` job key
  and gives it no `name:` override, because this classifier matches the review
  check by that literal string and an override would silently wedge every
  Dependabot lane at `awaiting-review` forever.

```bash
bash scripts/ralph/test_fleet.sh
bash scripts/ralph/test_pick_next.sh
bash scripts/ralph/test_pr_ready.sh
```

## Failure modes and how they're handled

| Scenario | Handling |
| --- | --- |
| Two "independent" issues touch the same file | Whichever merges first wins; the other goes `BEHIND`, lazily syncs main in, re-greens, then merges. A sync conflict ⇒ drops to Gate 1. Never a broken merge. |
| A slow lane would stall a fast one | It can't — lanes are independent; a ready lane merges immediately and its slot refills without waiting on any sibling. |
| A worker crashes / abandons an issue | `reconcile` releases it once its PR closes; an un-PR'd stale worktree is re-detected and either resumed or released on the next wake. |
| Fleet silts up with merged work | `reconcile` at the top of every wake GCs merged/closed worktrees. |
| A genuinely serial issue | Label it `solo`; it runs alone and blocks fills until done. |
| Want to disable parallelism | `parallel_enabled: false` in `state.json`. |
