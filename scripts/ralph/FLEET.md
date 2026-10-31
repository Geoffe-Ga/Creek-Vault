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
  for free) and each merge is **conflict-free**: a lane merges when
  `scripts/ralph/pr-ready.sh` prints `ready` — `LGTM` + CI-green on its current
  head + no merge conflict with `main` (`mergeStateStatus` `CLEAN` or `BEHIND`).
  **Being behind `main` is not a blocker** (owner rule, 2026-10-31: "If there are
  no merge conflicts then it is fine to merge a pr that passed all other
  gates"), so a reviewed lane never syncs just because a sibling merged first,
  and `pr-ready.sh` never asks the compare API, the inert-files rule, or
  `main-health.sh` about it. A lane that **conflicts** (`DIRTY`/`CONFLICTING`)
  reads `behind` and **merges `main` into its branch and resolves (by merge, not
  rebase — a plain push updates the PR and re-runs CI, never a force-push)**,
  then merges on a later wake once green again; one that cannot resolve cleanly
  **drops to Gate 1**. `UNKNOWN` (GitHub still computing mergeability) is "not
  yet known" and never clears.
  **The one exception is `ready-unreviewed`.** A reviewless Dependabot bump has
  no reviewer, and green CI against **current** `main` is its review substitute,
  so the previous currency rules still bind it unchanged: `behind_by == 0`, or
  behind but provably inert (#1137) with the `push: main` backstop green
  (`main-health.sh`, #1159; otherwise `main-not-green` — wait, do **not** sync).
- **Never wait on the slowest lane.** Whichever lane is ready merges immediately;
  the slot it frees refills at once. A fast lane at Gate 4 never waits for a slow
  lane at Gate 1.

The result: an imperfect independence guess costs at most a conflict
resolution — it can **never** merge conflicting code, and it **never** stalls a
ready lane behind a slow one. A behind lane's green was proven on an older
`main`; the `push: main` CI run is the backstop for any semantic (non-textual)
interaction between two conflict-free changes.

```
pick optimistically ──▶ N lanes build in parallel (isolated worktrees)
        │
   a lane goes LGTM+green ──▶ conflict-free (behind or not)? ──▶ merge NOW, refill its slot
        │                 ──▶ conflicted? ── merge main in + resolve ── re-green ── merge next wake
   unresolvable conflict? ──▶ that lane drops to Gate 1 (never a forced merge)
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
   + green + no merge conflict — `CLEAN` or `BEHIND`) merges *now*, serialized;
   a `behind` lane (a conflict, or a not-current reviewless bump) syncs first and
   merges on a later wake. A
   ready lane never waits for a slow one. `optout` lanes are skipped entirely.
3. **Advances failing lanes** — a `ralph-worker` is dispatched into the worktree
   of any PR that needs a fix (CI failure → `ci-debugging`; `CHANGES_REQUESTED` →
   `address-feedback`).
4. **Refills every open slot** — while `fleet.sh free > 0` and `pick-next.sh`
   yields a compatible issue, `assign` (or, for a `dependencies` issue already
   riding a bot PR, `adopt`) a worktree and launch a `ralph-worker`.
5. **Arms per-lane wakes (platform-aware)** — background workers wake it on
   their own completion regardless of platform. On a **remote/webhook-capable**
   session, each in-flight PR is `subscribe_pr_activity`-subscribed and an
   **adaptive** `ScheduleWakeup` backstops the transitions webhooks don't
   deliver: ~180s while any lane's PR is in CI/review (bounding a dropped
   webhook, a `behind→ready` flip, or a sibling-merge staleness — all
   event-less — to ~3 minutes), the long ~1200–1800s fallback when every lane
   is still building. On a **local terminal** session (no webhook MCP), it
   launches `scripts/ralph/watch-pr.sh <PR>` as a background task per
   in-flight PR — pidfile-idempotent, and the watcher's exit (the moment
   `pr-ready.sh`'s token settles) IS the wake — with the long fallback kept
   as the safety net. Then it ends the turn.

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
mechanism. Correctness is the serialized, conflict-free merge (resolve +
re-green when conflicted) described above.

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
| `assign <N> <slug>` | Create/reuse a worktree off `origin/main`; prints its path; refuses when full. Provisions the lane's `creek-tools/.venv` (`uv sync --all-extras`, idempotent) so the worker's first gate runs against real dev deps; refuses non-zero on a provisioning failure, leaving the worktree in place so a retry repairs it. |
| `adopt <N> <PR>` | The bot-PR variant of `assign`: create/reuse a worktree for issue N attached to PR `<PR>`'s **existing** head branch (e.g. Dependabot's), so fixes push there instead of opening a second PR. Refuses a fork PR (its branch is not pushable), a local branch diverged from `origin/<ref>`, and a full fleet. Provisions the lane's venv exactly as `assign` does. |
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
| `ready` | `LGTM` fresh + CI green + no merge conflict (`mergeStateStatus` `CLEAN` or `BEHIND` — being behind `main` is not a blocker). Merge now. |
| `ready-unreviewed` | CI green (with a real non-review `SUCCESS`, not just skipped checks) + `CLEAN` + `behind_by == 0`, but no review gate can ever exist — Dependabot authored the PR and pushed HEAD, so every review-rollup entry is `SKIPPED`. Report it; do not merge without the repo owner's OK — see `ralph-tick.md` Step 1 for the full reasoning and what would have to change first. |
| `behind` | Green, but not clearable without a sync: a merge **conflict** (`DIRTY`/`CONFLICTING` — merge `main` in and resolve), mergeability not yet computed (`UNKNOWN`), another non-mergeable state, or a `ready-unreviewed` bump that is not current. A reviewed lane is **never** `behind` merely for being behind. Sync (`fleet.sh sync`) and re-green. |
| `main-not-green` | A would-be `ready-unreviewed` bump, the lane is behind, and the `push: main` CI run that justifies merging while behind (#1157) is **not** green — red, still running, or unreadable (`scripts/ralph/main-health.sh`). The relaxation is suspended for the duration: **wait, and do not sync** — a sync would import the breakage, burn a CI round, and re-report as this lane's own `ci-failed`. Fails closed: a missing or non-executable helper holds the lane too. A `behind_by == 0` lane never asks and still merges, so the PR that fixes `main` is never blocked (#1159). |
| `review-quota-exhausted` | **Retired 2026-10-31 — no longer emitted.** It held a reviewed `CLEAN` lane that would otherwise sync, so the sync would not spend an LGTM an exhausted reviewer could never re-issue. Under the behind-is-not-a-blocker rule that lane is `ready` and merges with no sync, and a conflicting lane's remedy is the sync (always exempt). Still in `watch-pr.sh`'s in-flight set, so a stale reader routes it to "wait". |
| `pending` | CI still running, or no checks registered yet. Wait. |
| `ci-failed` | A **non-review** check is **positively proven** failed or errored — the rollup shows at least one failing entry that isn't the reviewer. Advance via `ci-debugging`. |
| `ci-unreadable` | The status rollup could not be read or reconciled with `gh pr checks`'s own exit code — a failed tally probe, a surplus field, a non-numeric count, an unclassifiable entry, or zero failing entries while `gh pr checks` exited non-zero (#1407, #1420). **Nothing is known to be red.** Non-terminal — it is in `watch-pr.sh`'s `IN_FLIGHT_TOKENS`, so the lane keeps polling and self-resolves: a genuinely red tree reads `ci-failed` on the next readable poll, a green one reads `ready`/`behind`/etc. **Do not dispatch `ci-debugging`** — that is the exact bug this token exists to stop (#1408). If the watcher instead prints `timeout ci-unreadable`, the rollup has been unreadable for ~30 minutes straight — treat it as tooling weather, re-check by hand, never convert it into a `ci-debugging` dispatch. |
| `review-failed` | Every failing check in the status rollup is `claude-review` itself, so CI is green and **the code needs no change** (#1200). The reviewer malfunctioned — rate limit, timeout, `cancel-in-progress` cancellation, or one of `code-review.yml`'s deliberate `exit 1` paths. **Do not dispatch `ci-debugging`**: re-run the failed review (`gh run rerun --failed <id>`) and re-classify. A rerun replays the old workflow file, which is fine for a rate-limit retry; once `code-review.yml` itself has changed, use the dispatch #1201 added — `gh workflow run code-review.yml -f pr_number=<PR>` — rather than an empty commit, which would invalidate the verdict the lane is waiting on. A strict **refinement** of `ci-failed`, never a replacement: an unreadable rollup, a failed probe, a surplus field, a non-numeric count, zero failing entries or any unclassifiable entry now read `ci-unreadable` instead — a non-terminal wait, not this token — so `review-failed` still can never swallow a real failure. A non-review check still **running** beside the failed review reads `pending`, not this. |
| `ready-comments` | a **fresh**, attested `COMMENTS` verdict (read with LGTM's fail-closed polarity) + every other `ready` condition. The reviewer signed off with non-blocking findings. **No worker, no iteration**: the orchestrator files each actionable finding as a follow-up issue with a severity-matched `P0`–`P3` label (`address-feedback` Step 1A), then merges exactly as for `ready` (`ralph-tick.md` Step 1). |
| `changes-requested` | CI green + a **fresh** `CHANGES_REQUESTED` verdict (posted after the HEAD commit). Gate 4 failed: advance via `address-feedback` (`ralph-tick.md` Step 2) now. A fresh `COMMENTS` verdict is never this token. A stale non-LGTM stays `awaiting-review`, and an unreadable verdict lookup fails closed (tooling error / `awaiting-review`) — never this token. |
| `awaiting-review` | CI green but no verdict for the current HEAD yet — none posted, or only a stale one (it predates HEAD, LGTM or not). Wait, or check for a hidden merge conflict masquerading as a missing review (see `ralph-tick.md` Step 1). |
| `optout` | `do-not-auto-merge` on the PR's own labels, or on the labels of the last issue it closes. Leave the lane **entirely** alone — no merge, no sync, no dispatch; a lane it already occupies stays occupied. |

### `main-not-green` vs. `review-quota-exhausted` (#1160) — the latter retired

`review-quota.sh`'s hold (#1160) only ever applied to a reviewed, `CLEAN` lane
that would otherwise sync because it was behind for a reason. Since 2026-10-31
such a lane is `ready` and merges without a sync, so `pr-ready.sh` no longer
asks; the helper and its own tests are unchanged. `main-health.sh` keeps its
fail-closed polarity (anything not `green` holds), now consulted only for a
behind `ready-unreviewed` bump.

Both are fail-closed in the *identical* sense — prefer the recoverable error —
and the recoverable error is simply the opposite one in each case. A future
reader who "harmonises" the two polarities either re-introduces #1160's bug or
wedges the fleet for days; `test_review_quota.sh`'s `never_exhausted()` sweep
and `test_pr_ready.sh`'s inverted sweep both pin their own direction.

## Tests

Seven offline suites cover the fleet, all run in CI by
`.github/workflows/ralph-recap-tests.yml` ("Ralph Tooling Tests" — one
workflow for everything under `scripts/ralph/`, shell and Python alike) on any
`scripts/ralph/**` change —
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
  a tag that shadows the branch name. Lane **provisioning** is covered against a
  knob-driven fake `uv` (the CI job never installs the real one): the success
  path, stdout/stderr discipline on both `assign` and `adopt`, the failure path
  (non-zero, empty stdout, an actionable diagnostic, the lock released, the
  worktree and its uncommitted work left alone, a partial `.venv` removed), a
  `uv` that exits 0 creating nothing, repair of a lane whose first provision
  failed, idempotency over a healthy lane, a missing `uv` (exit 2), and that a
  slow sync does not block a concurrent lane start.
- `scripts/ralph/test_pick_next.sh` stubs `gh` and exercises the picker's
  parallel-awareness: first-worker-lowest, worktree exclusion, in-flight-PR
  exclusion, the `solo` guard (candidate and active), the same-epic guard, the
  `parallelizable` override, and `RALPH_RESPECT_EPICS=0`.
- `scripts/ralph/test_pr_ready.sh` stubs `gh` and exercises the merge-readiness
  classifier: CI exit-code mapping, `mergeStateStatus`, the stale-verdict guard
  (an LGTM comment posted before the current HEAD push does not count), the
  conflict-only merge gate (a reviewed lane behind `main` — however far, behind
  a risk-surface change, with `main` red, or with the compare API down — is
  `ready`/`ready-comments` and never asks; `DIRTY`/`CONFLICTING` is `behind`;
  `UNKNOWN`/`BLOCKED` never merge), the
  `optout` hold on both the PR and its linked issue (including that an
  *undeterminable* hold exits 2 rather than reading as "no hold", and that the
  **last** `Closes #N` wins over an upstream changelog's `Fixes #N`), the full
  `ready-unreviewed` matrix including its `behind_by` freshness guard and every
  one of its fail-closed answers and the `main-not-green` precondition (a
  behind-but-inert bump holds when `main`'s CI is red / pending / unreadable, a
  `behind_by == 0` bump still clears while `main` is red, and a missing or
  non-executable `main-health.sh` sibling is never mergeable), and that the
  retired `review-quota-exhausted` hold is never emitted and `review-quota.sh`
  is never asked. And — via sentinel files — that the compare and main-health
  probes stay **lazy**: no reviewed, pending, red, or held lane pays for them.
  Two assertions are cross-file: they pin that
  `.github/workflows/code-review.yml` still declares the `claude-review` job key
  and gives it no `name:` override, because this classifier matches the review
  check by that literal string and an override would silently wedge every
  Dependabot lane at `awaiting-review` forever.
- `scripts/ralph/test_watch_pr.sh` stubs `pr-ready.sh` (a sequence-driven fake
  next to a copy of the script under test) and `gh`, and exercises the local
  per-lane hot watcher: pidfile idempotence (`already-watching` on a live pid,
  takeover of a stale/garbage one, removal on exit), settling on the first
  token outside `pending`/`awaiting-review`/`main-not-green`/
  `review-quota-exhausted`/`ci-unreadable` (including that `changes-requested`
  falls out of the in-flight set and wakes promptly, that `review-failed` does
  the same for the same reason — it is actionable, so sleeping on it would burn the full
  ~30-minute timeout on a state nothing resolves by itself (#1200) — and that
  `main-not-green` stays IN it — a
  watcher that exited on it would be relaunched and exit again, busy-waking the
  whole fleet for as long as `main` stayed red), the same busy-wake pin for
  `review-quota-exhausted` (staying in-flight through a poll and only exiting
  once the token flips to `behind`, and timing out as a wait state — not a
  settled token — when the quota window outlasts `TIMEOUT`), the same pin again
  for `ci-unreadable` (in-flight because no action is available while nothing is
  known to be wrong; exiting on it would busy-wake the fleet on every transient
  probe failure AND route the orchestrator into dispatching a fix worker at a
  possibly-green tree, which is #1408 itself — plus that a durable red still
  surfaces as `ci-failed` on the next readable poll, and that a rollup unreadable
  for the whole window times out as `timeout ci-unreadable` rather than stalling
  forever), `gone` on a
  merged/closed PR, `timeout <last-token>` at the deadline, and that transient
  `pr-ready.sh` / `gh` failures never kill the watcher — every wait outcome
  exits 0.
- `scripts/ralph/test_main_health.sh` stubs `gh` and exercises the `main` CI
  circuit breaker: which conclusions count as evidence (`success` → `green`;
  `failure`/`timed_out`/`startup_failure` → `red`; cancelled / skipped /
  neutral / action_required / stale / empty are skipped, not answered on), the
  anti-serialization pin (a run in flight over a completed success is still
  `green`, or this gate would hold every behind lane for ~14 minutes after
  every merge), the fail-closed sweep (empty list, non-zero `gh`, unparseable
  output, a surplus 6th field, an all-inconclusive window — never `green`,
  always exit 0, always one bare token), exactly one `gh` call on every path,
  stdout purity with attribution on stderr, and that a red verdict attributes a
  blame **range** `<newest green sha>..<red sha>` (or says the culprit is
  unattributable rather than faking one). Two assertions are cross-file: they
  pin that `.github/workflows/ci.yml` keeps a `push:` trigger including `main`
  and never sets `cancel-in-progress: true` unconditionally — either edit
  deletes the very run this gate reads, silently.
- `scripts/ralph/test_review_quota.sh` stubs `gh` and exercises the reviewer-
  availability probe (#1160), `main-health.sh`'s sibling and deliberate polar
  opposite: the three real payloads captured from live incidents (PR #1158's
  actual rejection → `exhausted`; the #1117 log's TWO `rate_limit_event`
  blocks with different `resetsAt` values, proving "last `resetsAt` in the log
  wins" is wrong; and the Aug-7 re-run of that same #1158 job, which concluded
  `success` with a full LGTM yet still carries `"overageStatus": "rejected"` and
  `"out_of_credits"` in its log — the false-positive pin any bare `rejected` /
  `overageStatus` / case-insensitive `status` grep would trip), the cheap-path
  proof that a `success` run never opens its log at all (`GH_CALLS == 1`, so
  that false positive is unreachable on the common path), the bounded reset
  horizon (a `resetsAt` dated more than 8 days out reads `unknown`, not
  `exhausted` — an unbounded horizon would let one forged line hold the fleet
  for years), the bounded log scan (`LOG_TAIL_LINES`, needed for correctness —
  a real rejection is always near the end — and for cost against bash 3.2's
  quadratic array indexing), and the cardinal `never_exhausted()` sweep: every
  malformed, missing, or merely-uncertain input answers `available` or
  `unknown`, never `exhausted` — the mirror of `test_main_health.sh`'s
  `not_green()` and the assertion this whole probe exists to protect. One
  assertion is cross-file: it pins that `.github/workflows/code-review.yml`
  keeps its `claude-review` job key and its `pull_request:` trigger — a rename
  makes this helper answer `unknown` forever and #1160's bug silently returns.
- `scripts/ralph/test_exec_bits.sh` asserts every `scripts/ralph/*.sh` is
  committed mode `100755` per `git ls-files -s` — the INDEX mode, so a local
  unstaged `chmod` can't fake it. These scripts are invoked by path
  (`scripts/ralph/watch-pr.sh <PR>`, ralph-tick.md Step 5), and CI runs the
  suites via `bash <file>`, so nothing else catches a script shipped `100644`
  exiting 126 on every fresh clone (issue #1096).

```bash
bash scripts/ralph/test_fleet.sh
bash scripts/ralph/test_pick_next.sh
bash scripts/ralph/test_pr_ready.sh
bash scripts/ralph/test_watch_pr.sh
bash scripts/ralph/test_main_health.sh
bash scripts/ralph/test_review_quota.sh
bash scripts/ralph/test_exec_bits.sh
```

## Failure modes and how they're handled

| Scenario | Handling |
| --- | --- |
| Two "independent" issues touch the same file | Whichever merges first wins. If the other now conflicts it reads `behind`, merges main in and resolves, re-greens, then merges (an unresolvable conflict ⇒ Gate 1); if it does not conflict it stays `ready` and merges. Never a conflicting merge. |
| A slow lane would stall a fast one | It can't — lanes are independent; a ready lane merges immediately and its slot refills without waiting on any sibling. |
| `main` itself goes red | Reviewed lanes are unaffected by `main`'s CI colour (being behind is not a blocker). For a behind `ready-unreviewed` bump the backstop that justifies merging it is dead, so it reads `main-not-green` and **waits** (they do not sync — that would import the breakage). Refill stops (a new lane off a broken `origin/main` fails Gate 2 locally on somebody else's bug), and one `ci-debugging` lane is dispatched at the attributed commit, guarded by an open `main is red at <sha7>` issue so repeated wakes do not re-dispatch. A `behind_by == 0` + green lane — the shape of the fix PR — still merges, closing the deadlock by construction for tree-borne breakage; a stale-but-green such lane can still miss a time/environment-triggered break (a freshly published advisory, an expired pin — see `main-health.sh`'s header for the observed instance), an accepted residual rather than a gap in the proof (#1159). |
| The `claude-review` reviewer runs out of quota (#1160) | No longer a hazard for a reviewed lane that is merely behind: it is `ready` and merges with no sync, so its LGTM is never spent. A conflicted lane must still sync, which needs a re-review once the quota resets; never `fleet.sh release` it meanwhile (issue #1180). `review-quota-exhausted` is retired (2026-10-31). |
| A worker crashes / abandons an issue | `reconcile` releases it once its PR closes; an un-PR'd stale worktree is re-detected and either resumed or released on the next wake. |
| Fleet silts up with merged work | `reconcile` at the top of every wake GCs merged/closed worktrees. |
| A genuinely serial issue | Label it `solo`; it runs alone and blocks fills until done. |
| Want to disable parallelism | `parallel_enabled: false` in `state.json`. |
