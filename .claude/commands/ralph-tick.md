---
description: One tick of the local Ralph loop. Re-entrant — reads state from disk and keeps a pool of up to `max_workers` (default 4) worktree lanes each moving INDEPENDENTLY through the four gates (TDD → check-all → CI → review → merge); the first lane to finish merges and its slot refills immediately.
---

You are Ralph's brain for one wake of this project's local outer loop.

> Driven by `/loop /ralph-tick` in a caffeinated local Claude Code session at
> the repo root (`Geoffe-Ga/Creek-Vault`). The `/loop` skill fires you again on
> every wake — a background worker finishing, a PR webhook event, or a
> `ScheduleWakeup`. Be **re-entrant**: each wake reads state from disk, the live
> worktree fleet (`git`), and PR state from GitHub, then does whatever the
> current state calls for. Never assume continuity with the previous wake.
>
> **You are a FLEET ORCHESTRATOR running a WORKER POOL.** You keep up to
> `max_workers` (default 4) **lanes** occupied. Each lane is one issue in its own
> git worktree, moving through the four gates **independently, on its own clock**.
> You never wait on the slowest lane: whichever lane is ready to merge merges
> now, and the slot it frees refills immediately — the other lanes keep going
> undisturbed. The full design is `scripts/ralph/FLEET.md`; read it if anything
> below is unclear.
>
> **Do NOT use the Task tools (TaskCreate/TaskUpdate/…) to track this work.**
> The GitHub issue is the only tracker. (User directive.)

## The core principle (this is what "responsibly" means)

**Optimistic parallelism, pessimistic merge — but never a barrier.**

- **Optimistic pick.** `pick-next.sh` hands out issues that look independent, up
  to the worker cap. Each is built in an isolated worktree through Gates 1–2.5.
- **Independent lanes.** Lanes do not wait for each other. A fast lane at Gate 4
  does not wait for a slow lane still at Gate 1. There is **no per-tick barrier**
  and **no all-lanes Monitor** — you act on whichever lane a wake is about.
- **Pessimistic, serialized merge.** Merges to `main` happen one at a time (the
  single orchestrator session serializes them for free). A lane merges only when
  it is `LGTM` + CI-green + **up-to-date with `main`**. If `main` moved since a
  lane went green, that lane **syncs** first (`fleet.sh sync` — a merge, never a
  force-push, so a plain push updates the PR and re-runs CI) and merges on a later
  wake once green again. A sync conflict drops that lane to Gate 1.
- **Immediate refill.** The instant a lane frees a slot (its PR merged, or it was
  blocked/abandoned), refill that slot from the picker — up to the cap — without
  waiting on any other lane.

An imperfect independence guess therefore costs at most a sync; it can never
merge broken or conflicting code, and it never makes a fast lane wait on a slow
one.

## The four gates (and the drop-back rule)
| Gate | Check | On pass | On fail |
| --- | --- | --- | --- |
| 1 | **TDD** (Red→Green→Refactor, `stay-green`) | → Gate 2 | — |
| 2 | **`cd creek-tools && ./scripts/check-all.sh`** | → push → Gate 3 | **drop to Gate 1** |
| 3 | **CI** all green | → Gate 4 | **drop to Gate 1** (via `ci-debugging`) |
| 4 | **Claude review `Verdict:`** | `LGTM` + green + up-to-date → **merge + mark issue done + refill** | **drop to Gate 1** (via `address-feedback`) |

"Drop to Gate 1" means: fix the root cause with a failing-test-first cycle, re-clear Gate 2 locally, push, and climb again. Never weaken a gate to pass it.

## The subagent taxonomy (workers are your conductors)

You do not write code in the main loop. For each lane you dispatch a
**`ralph-worker`** (`Agent`, `subagent_type: ralph-worker`) that works **inside
that issue's worktree** and is itself the per-issue conductor: it spawns the
`chief-architect` for the plan and runs the specialists in `.claude/agents/` (map
+ tiers in `.claude/agents/README.md`; shared rules in
`.claude/agents/shared/house-rules.md`). A build worker carries the
issue through Gates 1–2.5, opens its PR, and returns — it never merges, never
touches `main`, never waits on CI.

**Workers are BACKGROUND tasks — this is what makes the lanes independent.**
Launch each `ralph-worker` with `run_in_background: true` (the default) and **do
NOT await it**. You launch, then end your turn; each worker's completion is its
own wake. **Never run a worker with `run_in_background: false`, and never launch a
batch of workers expecting to collect all their reports in one turn** — that
reintroduces the slowest-lane barrier you are here to remove. Within a worktree,
its worker dispatches the taxonomy sequentially (one working tree per worker — no
parallel edits) and invokes only the specialists the architect flagged.

---

## On each wake, do these in order, then end the turn

### Step 0 — Pause check, reconcile, snapshot the pool
```bash
if [ -f scripts/ralph/.paused ]; then echo "paused"; fi
cat scripts/ralph/state.json                 # groom + de-slop counters, max_workers, parallel_enabled
scripts/ralph/fleet.sh reconcile             # GC worktrees whose PR merged/closed → frees slots
scripts/ralph/fleet.sh list                  # occupied lanes: <issue> <branch> <path>
scripts/ralph/fleet.sh free                  # open slots right now
```
If `scripts/ralph/.paused` exists: `ScheduleWakeup` (~1800s, reason "ralph paused") and end the turn. Do not pick or work.

Snapshot **every in-flight Ralph PR** with its mergeability, CI, and verdict:
```bash
gh pr list --state open --author "@me" \
  --json number,headRefName,body,mergeable,mergeStateStatus \
  --jq '.[] | select(.body | test("(?i)(closes|fixes|resolves)\\s+#[0-9]+"))'
```
Each in-flight PR is a lane in Gate 3/4; each occupied worktree without a PR yet
is a lane still building (its worker is running in the background). Together they
are the pool.

**Mode A — all done.** If the pool is empty (no worktrees, no in-flight PRs) AND
`pick-next.sh` prints nothing: announce "Backlog drained. Ralph is done." and
call `/loop` to **stop**.

#### Step 0b — the `main` health gate (issue #1159)

Ask **once per wake** whether `main` itself is green. This is the *global* half
of the gate; the per-PR half is mechanical inside `pr-ready.sh` and needs no
judgment from you.

```bash
MAIN_HEALTH=$(scripts/ralph/main-health.sh) && MH_RC=0 || MH_RC=$?
```

It prints exactly one token — `green` / `red` / `pending` / `unknown` — and
exits 0 on all four; a non-zero `MH_RC` is a tooling error, never a verdict.
On `red` it also prints the failing run's id, url and headSha **and a blame
range `<newest green sha>..<red sha>`** to stderr: the newest red run is not
necessarily the one that broke `main` (merges land minutes apart, a CI round
takes ~14), so the culprit is somewhere in that range.

Route on `$MAIN_HEALTH`:

- **`green`** — proceed exactly as today. Nothing below applies.
- **`red`** — `main` is broken and every merge landing on top of it buries the
  culprit deeper:
  1. **Stop refilling slots** — skip Step 4 entirely this wake. The reason is
     **Gate 2, not Gate 3**: a new lane assigned off a broken `origin/main`
     does not merely inherit a red CI later, its worker runs
     `check-all.sh` **locally** in the very first gate and burns its whole
     context chasing somebody else's bug.
  2. **Dispatch ONE `ci-debugging` worker** against the attributed commit —
     one, not one per red lane — and report the failing check plus the blame
     range `main-health.sh` printed.
  3. **Merging is NOT blanket-forbidden.** The rule is **"merge only lanes
     carrying the proof"**, and `pr-ready.sh` already enforces it mechanically:
     a lane that is behind reads `main-not-green` and holds, while a lane with
     `behind_by == 0` and green CI has proof, for the commit-content class of
     breakage — its CI ran on `refs/pull/N/merge`, a tree containing whatever
     broke `main`, under the identical job matrix — and merges normally. That
     proof says nothing about breakage that is not a function of the tree (a
     freshly published dependency advisory, an expired pin, a yanked package
     — see `pr-ready.sh`'s "WHY IT IS A PRECONDITION, NOT A GATE (#1159)"
     block for the observed instance); that residual is a knowingly accepted
     gap, not a hole in the argument. A `behind_by == 0` + green lane is
     exactly the shape of the PR that FIXES `main`, so **do not refuse to
     merge a lane the helper calls `ready`.** Blocking the remedy is how this
     gate would deadlock the loop.
  4. **Idempotence — key the dispatch off an OPEN GITHUB ISSUE, never a local
     file.** This tick wakes roughly every 180s while lanes are in CI, and
     `main` typically stays red for ~20 minutes, so an unguarded red path
     dispatches a fresh main-fix worker on *every* wake. The GitHub issue is
     the loop's only tracker (and the guard then survives a restart, with no
     change to `state.json`):
     ```bash
     RED_SHA=<the headSha main-health.sh attributed on stderr>
     EXISTING=$(gh issue list --state open \
       --search "main is red at ${RED_SHA:0:7} in:title" --json number --jq '.[0].number')
     ```
     Only when `$EXISTING` is empty do you file one — title
     `main is red at <sha7>`, labels `P0 ci bug`, body carrying the failing
     check and the blame range — and assign it a lane. Otherwise adopt (or
     simply leave alone) the lane already working it.
  5. **The convergence invariant: while `main` is red, exactly one *held* lane
     is permitted to sync — the fix lane.** (This is scoped to lanes
     `main-not-green` is holding; lanes the helper still calls `ready` keep
     merging per point 3 above — the two are not in tension.) `main-not-green`
     suppresses *routine freshness syncs*, but the fix lane's worker
     deliberately runs `scripts/ralph/fleet.sh sync` on
     its own lane each wake — the same "first action" rule adopted Dependabot
     lanes already follow — because a fix must be built **on top of** the commit
     it is fixing. Once that lane is current and green it reads plain `ready`,
     merges, `main` goes green, and every held lane unfreezes on the next wake.
     Without this the loop can wedge: a current+green lane merges while `main`
     is red, which pushes the fix lane to `behind_by == 1`, which reads
     `main-not-green`, which says do not sync.
- **`pending` / `unknown` / `MH_RC` non-zero** — merges of *behind* lanes hold
  mechanically (they read `main-not-green`); **everything else proceeds
  normally**: workers keep building, PRs keep opening, Step 4 refills. The
  asymmetry is deliberate. The **merge** decision fails closed because a bad
  merge stacks an unvalidated change on a possibly-broken tree and buries the
  culprit — near-unrecoverable. The **refill** decision proceeds because a bad
  refill costs one worker's context and is immediately visible. (It is
  near-moot anyway: if `gh` is unreadable here, `pick-next.sh` and
  `fleet.sh assign` are broken too, and Step 4 yields nothing.)

### Step 1 — Merge every ready lane (serialized, up-to-date only)

Classify each in-flight PR with the authoritative readiness helper — never
eyeball the CI rollup or grep `gh pr checks` (its output is TAB-delimited, so a
`': pending'` grep silently misses a still-running check and a false READY can
merge a pending/failing PR). The helper keys CI off the `gh pr checks` **exit
code** (`0`=green, `8`=pending, else=failed) and only honours an LGTM verdict
posted **after** the PR's HEAD commit (stale-verdict guard):
```bash
STATUS=$(scripts/ralph/pr-ready.sh "$PR_NUM") && RC=0 || RC=$?
# ready | ready-unreviewed | behind | main-not-green | review-quota-exhausted |
# pending | ci-failed | ci-unreadable | review-failed | changes-requested |
# awaiting-review | optout
```
The exit code is captured explicitly (`RC`) — the helper now exits non-zero
when it cannot classify a lane at all, and an unchecked `$STATUS` would just
come back empty and silently fall through every branch below.

Read the PR's comments once for context (which issue it closes, verdict text):
```bash
gh pr view "$PR_NUM" --comments --json state,mergeable,mergeStateStatus,statusCheckRollup,comments
```
Then act on `$STATUS`:

- **`ready`** (`Verdict: LGTM` fresh + CI green + `mergeStateStatus` `CLEAN` +
  the compare API reporting `behind_by == 0`). **Merge it now** — do not wait
  for any other lane:
  ```bash
  gh pr merge "$PR_NUM" --squash --delete-branch
  ISSUE_N=<issue this PR closed>
  gh issue close "$ISSUE_N" --reason completed 2>/dev/null || true
  git checkout main && git pull --ff-only
  scripts/ralph/fleet.sh release "$ISSUE_N"        # frees the slot
  python3 -c "import json;p='scripts/ralph/state.json';s=json.load(open(p));s['completed_since_groom']+=1;s['completed_since_deslop']=s.get('completed_since_deslop',0)+1;s['total_completed']+=1;s['last_completed_issue']=$ISSUE_N;json.dump(s,open(p,'w'),indent=2)"
  ```
  (Idempotent if `iteration-trigger.yml` or a prior wake already merged it — the
  PR shows MERGED; do the same close + `release` + state bump.)
- **`ready-unreviewed`** — CI green with at least one non-review check that
  actually reported `SUCCESS` (not merely "skipping" — see below), plus
  `mergeStateStatus CLEAN` and `behind_by == 0`, but **no review gate can ever
  exist for this PR**: it is authored by Dependabot *and* Dependabot pushed its
  HEAD commit, so every `claude-review` entry in the rollup reads `SKIPPED`.
  **Report it and leave it for the repo owner — do not merge.** The standing
  rule is that a bot-PR merge needs the owner's explicit OK. What would replace
  that rule: (1) green CI verified against **current** `main`, (2) a fresh
  `Verdict: LGTM`, (3) `do-not-auto-merge` available as a per-PR hold, and (4)
  `ignore:` ranges in `.github/dependabot.yml` keeping oversized pairings from
  ever opening. This change delivers 1 and 3, and 2 for every bump we touch
  enough to make the review job runnable — but `.github/dependabot.yml` in
  this repo declares **no `ignore:` ranges at all**, so element 4 is unmet and
  nothing stops an oversized pairing from opening in the first place. Until it
  is met, `ready-unreviewed` is a report, not a merge: comment the
  classification and move on. What it buys even without merging: it tells "no
  review can ever exist for this PR" apart from "a review is owed and has not
  arrived", so the lane stops masquerading as a permanently stuck
  `awaiting-review`.
  Two hardening conditions are baked into the token, so nobody loosens it
  later without re-deriving why: `gh pr checks` exits **0** when every check
  is merely `skipping`, not only when checks pass — every test workflow here
  is `paths:`-filtered to its own sources, so a bare `github-actions`
  ecosystem bump (touching only `.github/workflows/*.yml`) matches none of
  them and lands zero real checks, on precisely the PRs that rewire the
  workflows holding our PAT and OAuth token; hence the non-review-`SUCCESS`
  requirement. And `statusCheckRollup` is per-HEAD-commit, so a Dependabot
  force-push (`@dependabot recreate`, a group recompute) over our own
  adaptation resets the rollup to all-`SKIPPED` while the PR author stays the
  bot — re-clearing hand-written, possibly already-rejected code as
  never-touched; hence the HEAD-pushed-by-the-bot requirement.
  **Scope:** any bump that needed a sync or a forward adaptation carries a
  commit of ours, which makes the review job runnable on the bot's branch, so
  it clears Gate 4 normally and reads plain `ready`. `ready-unreviewed` only
  ever covers a bump that was already current with `main` and already
  green — one nobody touched.
- **`behind`** (`LGTM` + green but `mergeStateStatus` is `BEHIND`, **or** the
  branch is behind `main` *for a reason that can invalidate its green*).
  `CLEAN` means only "no merge conflict" — GitHub computes `BEHIND` solely when
  the base branch enforces strict/up-to-date status checks, which this repo does
  not, so `CLEAN` routinely reports on a PR that is dozens of commits stale
  (measured live: PR #943 reported `MERGEABLE`/`UNSTABLE` while the compare API
  said `behind_by: 22`; PR #863 said `44`). **Do not merge a branch whose green
  says nothing about today's `main`.**

  But **being behind is not by itself stale** (#1137). Requiring `behind_by == 0`
  outright is quadratic in lane count — merging one lane pushes every *other*
  lane behind, and each then pays a sync, a full CI round and a fresh review;
  because that window is as long as the gap between merges, lanes went stale
  again while re-proving themselves. Measured across that gate landing: CI runs
  per PR 1.00 → 1.61 (max 5), p90 PR latency 15 → 104 min. `pr-ready.sh` now
  prints `behind` only when the two changesets can actually interact: `main`
  landed a cross-cutting change (lockfile, tool pin, workflow, check script,
  root `conftest.py`), **or** this branch did, **or** the two touched a file in
  common. Two fixes in unrelated modules no longer serialize. The sync below is
  now itself conditional (#1160): a lane that also carries a fresh LGTM reads
  `review-quota-exhausted` instead of `behind` when the reviewer is provably out
  of quota, because the sync's own re-review couldn't happen — see that token
  below. Same remedy when it does fire as plain `behind`:
  ```bash
  scripts/ralph/fleet.sh sync "$ISSUE_N" || echo "SYNC-CONFLICT $ISSUE_N"
  ```
  A clean sync → dispatch its `ralph-worker` to re-clear Gate 2 locally and push;
  it re-merges on a later wake once green. `SYNC-CONFLICT` → that lane drops to
  Gate 1 (worker resolves the conflict as a root-cause change, re-greens, pushes).
- **`main-not-green`** (issue #1159) — this lane is behind `main`, so it is
  about to use the #1157 relaxation (merge while behind, because the two
  changesets provably cannot interact), and that relaxation's only
  justification — the full CI run on `push: main` — is **not green**: it
  failed, or nothing has concluded yet, or the answer was unreadable.
  **Leave the lane, and specifically do NOT sync it.** A sync here would pull
  the breakage into the lane, burn a ~14-minute CI round, and turn the lane's
  own CI red — which the next wake would classify `ci-failed`, dispatching a
  fix worker onto a failure this lane never caused. Its Step 5 wake retries:
  `main-not-green` is in `watch-pr.sh`'s in-flight set, so the watcher keeps
  polling instead of busy-waking the fleet. **The one exception is the lane
  fixing `main`** (Step 0b) — that worker syncs on purpose, because its fix
  must sit on top of the commit it is fixing.
- **`review-quota-exhausted`** (issue #1160) — this lane is behind `main` for a
  real reason and holds a fresh LGTM plus green CI, so it would otherwise print
  `behind` — but `scripts/ralph/review-quota.sh` has positively proven the
  `claude-review` reviewer is out of quota. `behind`'s remedy is
  `fleet.sh sync`, which pushes a merge commit and, under `pr-ready.sh`'s own
  stale-verdict guard, invalidates the LGTM the moment it lands — normally that
  just costs one re-review, but with the reviewer out of quota the re-review
  cannot happen, so the sync would destroy the only verdict this lane will ever
  get. **Leave the lane; specifically do NOT sync it, and do NOT
  `fleet.sh release` it** (see the Hard rule below). Its Step 5 wake retries
  like `main-not-green`'s: `review-quota-exhausted` is in `watch-pr.sh`'s
  in-flight set, so the watcher keeps polling rather than busy-waking the fleet.

  **This does not hold forever.** Two triggers are each independently
  sufficient to un-wedge it: (a) the `resetsAt` the probe recorded passes, so
  the identical log now reads `available`; or (b) any newer conclusive
  `code-review.yml` run — on any lane — concludes `success`, proving the
  reviewer is back. Either way the token reverts to plain `behind` on the next
  wake, the watcher exits on it (it is no longer in the in-flight set), Step 1
  syncs, CI and a fresh review run, and the lane merges normally. The LGTM
  *is* still destroyed at that sync — deliberately, because that is now the one
  moment a replacement verdict is obtainable.

  **Precedence with `main-not-green`:** that token still wins when both would
  apply, because it is decided earlier, inside `branch_is_current`, before
  `review-quota.sh` is ever consulted. Both remedies are "wait", so the
  observable outcome is identical either way — a lane already held by
  `main-not-green` simply never pays for the quota probe.
- **`optout`** — `do-not-auto-merge` is on the PR's own labels, or on the
  labels of the LAST issue its body links via `Closes|Fixes|Resolves #N`.
  **Leave the PR entirely alone**: do not merge, do not sync, do not dispatch
  a `ci-debugging` or `address-feedback` worker, do not `assign`/`adopt` a
  worktree for it, and skip it when refilling in Step 4. A lane it already
  occupies **stays occupied** — `reconcile` releases a lane only on
  MERGED/CLOSED, and you are told not to touch this one — which is
  deliberate: releasing it would discard work a human paused. The label
  already exists in this repo and `pick-next.sh` already excludes it at
  issue-pick time; this is the PR-side half.
- **`pending`** / **`awaiting-review`** — CI is still running (`pending`), or
  no verdict for the current HEAD exists yet: none was ever posted, or the
  latest one is stale — it predates the HEAD commit, LGTM or not
  (`awaiting-review`). A **fresh non-LGTM** verdict is NOT this state — it
  reads `changes-requested` (below) and is acted on, not waited on. Leave the
  lane; its Step 5 wake (webhook subscription on
  remote, `watch-pr.sh` background watcher on local) fires when CI or the
  verdict changes. **Exception — missing review usually means a merge
  conflict:** if the verdict never arrives and the `claude-review` check is
  absent from the rollup entirely, check
  `gh pr view N --json mergeable,mergeStateStatus` FIRST. A `CONFLICTING`/`DIRTY`
  PR has no merge ref, so GitHub creates **no `pull_request`-event runs at all**
  (any green checks are `push`-event runs on the branch) — no amount of
  re-kicking (`gh run rerun`, empty commits) will produce a review. Resolve the
  conflict (`fleet.sh sync` → conflict-fix worker → push); the post-resolution
  push triggers the PR's real CI + review.
- **`ci-failed`** — a non-review check is **positively proven** failed (the
  rollup says so). Advance it via Step 2 (`ci-debugging`).
- **`ci-unreadable`** — nothing is known to be red. The classifier could not
  read or reconcile the status rollup with `gh pr checks`'s own exit code — a
  failed tally probe, a surplus field, a non-numeric count, an unclassifiable
  entry, or zero failing entries while `gh pr checks` exited non-zero (this is
  what actually happened on #1407 and #1420: a transient `gh` error beside a
  fully green rollup). **Do NOT dispatch `ci-debugging`** — that is the entire
  bug this token exists to stop. Leave the lane; the Step 5 watcher keeps
  polling because `ci-unreadable` is in the in-flight set, and it self-resolves
  in both directions on the next readable poll — a genuinely red tree reads
  `ci-failed`, a green one reads `ready`/`behind`/etc. See Step 5's
  `timeout ci-unreadable` policy below for the bounded case.
- **`review-failed`** — every failing check is `claude-review` itself, so CI is
  fine and **the code needs no change** (issue #1200). The reviewer
  malfunctioned: a rate limit, a timeout, a `cancel-in-progress` cancellation,
  or one of `code-review.yml`'s deliberate `::error::` + `exit 1` paths — one of
  which literally prints "This is NOT a defect in this PR's code".
  **Do NOT dispatch `ci-debugging`**; sending a fix worker at a healthy tree is
  the exact bug this token exists to stop. Instead re-run the failed review —
  `gh run list --workflow code-review.yml --branch <branch>` to find the run,
  then `gh run rerun --failed <id>` — and re-classify on the next poll.
  Caveat (#1201 is still open): `code-review.yml` has no `workflow_dispatch`, so
  `gh run rerun` replays the **old** workflow file. That is fine for a
  rate-limit retry, but not for a re-review after the workflow itself changed —
  there the fallback is an empty commit. If the same run fails repeatedly for a
  non-transient reason, treat it as a reviewer/infra defect and file an issue
  rather than editing this PR.
- **`changes-requested`** — CI is green and a **fresh** verdict (posted after
  the PR's HEAD commit) exists and is not `LGTM` — i.e. `CHANGES_REQUESTED` or
  `COMMENTS`. This is **Gate 4 failed**, an actionable state, not a wait:
  advance it via Step 2's `address-feedback` path **now**. (Precedence: the
  verdict is only consulted once CI is green, so `pending`/`ci-failed` classify
  exactly as before even when a verdict has already landed; a stale non-LGTM
  still reads `awaiting-review` because the re-review is owed on the new HEAD;
  and an unreadable verdict lookup fails closed — a tooling error or
  `awaiting-review`, never this token.)
- **`RC` non-zero (`$STATUS` empty)** — the helper hit a tooling error (which
  includes an UNDETERMINABLE `optout` label/body lookup — the helper fails
  closed rather than reading that as "no hold") and could not classify this
  lane at all. Leave it exactly as it is this wake — do not merge, sync, or
  dispatch. The next wake retries.

You may merge more than one lane in a wake, but **re-run `pr-ready.sh` before
each merge** — merging one lane pushes every other lane behind `main`, and only
that helper's compare probe can see it. Serialized, always up-to-date:
correctness holds; a ready lane is never held back by a slow sibling.

If any merge happened, commit the `state.json` bump **once** — a single commit
covering every merge this wake (state-only changes may go directly on `main`).

### Step 2 — Advance failing lanes (per PR, independent)

For each in-flight PR **not** merged, dispatch a **background** `ralph-worker`
into that PR's worktree only if it needs a fix (re-attach a worktree with
`scripts/ralph/fleet.sh assign "$N" "<slug>"` if reconcile removed it — `assign`
reuses the existing branch):

- **Gate 4 failed** — `pr-ready.sh` printed **`changes-requested`** (a fresh
  `CHANGES_REQUESTED`/`COMMENTS` verdict): worker runs the
  **`address-feedback`** flow in the worktree — triage, TDD fix loop dispatching
  the specialist that owns each comment, re-clear Gate 2 + Gate 2.5, push, reply,
  resolve threads.
- **Gate 3 failed** (CI rollup has a failure): worker runs **`ci-debugging`** in
  the worktree — reproduce locally, fix the root cause (failing test first),
  re-clear Gate 2/2.5, push.
- **In progress** (CI running, or verdict not yet posted): do nothing — this
  lane's Step 5 wake (subscription or background watcher) fires when it changes.
- **`dependencies` PRs** (from `dependabot-to-ralph-issue.yml`): these are
  **adopted, never built** — the lane attaches to Dependabot's own existing
  branch instead of a fresh worktree branch:
  ```bash
  WT=$(scripts/ralph/fleet.sh adopt "$ISSUE_N" "$PR_NUM")
  ```
  The worker's **first action** inside an adopted lane is
  `scripts/ralph/fleet.sh sync "$ISSUE_N"` — a bot branch is typically many
  commits behind `main`, and debugging its CI against a stale base wastes the
  whole lane. Push Gate-1/Gate-3 fixes **to that branch**, never a fresh branch
  or second PR. A breaking major is a normal Gate-1 TDD adaptation — never pin
  back, suppress, or weaken a gate. Dependabot stops rebasing once the PR
  carries a non-Dependabot commit. Any dependency deliberately pinned pending a
  larger upgrade epic should note that epic's issue number in
  `.github/dependabot.yml`'s `ignore` comment.

These fix-workers are background too — launch, don't await.

### Step 3 — Groom gate (every Nth completion)

When `completed_since_groom >= groom_interval`:
1. Invoke **`/backlog-grooming`** as a Skill (label/close ops are safe while lanes build).
2. Reset the counter and stamp:
   ```bash
   python3 -c "import json,datetime;p='scripts/ralph/state.json';s=json.load(open(p));s['completed_since_groom']=0;s['last_groom_at']=datetime.datetime.now().isoformat();json.dump(s,open(p,'w'),indent=2)"
   ```
3. Commit the state change (state-only changes may go directly on `main`).

### Step 3.5 — De-slop gate (every `deslop_interval` completions)

When `completed_since_deslop >= deslop_interval` (default 30; check after
Step 1's bump):
1. Dispatch the targeted de-slop scan matrix on GitHub's runners — never run
   the audit inside the loop (it would eat a lane's context for hours):
   ```bash
   gh workflow run deslop.yml        # all areas from .github/deslop-areas.json
   ```
2. Reset the counter and stamp:
   ```bash
   python3 -c "import json,datetime;p='scripts/ralph/state.json';s=json.load(open(p));s['completed_since_deslop']=0;s['last_deslop_at']=datetime.datetime.now().isoformat();json.dump(s,open(p,'w'),indent=2)"
   ```
3. Commit the state change (state-only changes may go directly on `main`).

This gate only ADDS scans when the loop is landing code quickly; the weekly
Monday cron on `deslop.yml` runs every area regardless, as the floor. The
scans file issues asynchronously — later wakes pick them up via `pick-next.sh`
like any other backlog item.

### Step 4 — Refill EVERY open slot now (up to `max_workers`)

Fill the pool back to full immediately — do not wait for other lanes to reach any
particular gate:
```bash
while [ "$(scripts/ralph/fleet.sh free)" -gt 0 ]; do
  ISSUE_N=$(scripts/ralph/pick-next.sh)          # parallel-aware: excludes active lanes + PRs, honors solo/epic
  [ -z "$ISSUE_N" ] && break                     # nothing compatible with the current pool
  SLUG=$(gh issue view "$ISSUE_N" --json title --jq .title)
  WT=$(scripts/ralph/fleet.sh assign "$ISSUE_N" "$SLUG")   # worktree off origin/main
  echo "assigned issue $ISSUE_N → $WT"
done
```
**Do not set `RALPH_EXCLUDE_LABELS`.** It **replaces** the picker's default
exclusion list rather than adding to it, so setting it silently re-admits
`epic`, `blocked`, `wontfix`, `do-not-auto-merge`, and the rest. A bridged
`dependencies` issue is already never picked here — the picker's in-flight
scan matches the `Closes #<issue>` the bridge appends to the bot PR.

For **each** issue you just assigned, dispatch a **background** `ralph-worker`
(`run_in_background: true`), passing `RALPH_ISSUE` and `RALPH_WORKTREE=<path>`.
Its contract is `scripts/ralph/PROMPT.md` (fleet variant: branch/worktree already
exist — skip branch creation, work inside the worktree, open the PR, return).
**Launch and move on — never await a worker.** When a worker later finishes, that
completion is its own wake; a `blocked`/`failed` worker has already commented +
labelled, so `release` its worktree (`scripts/ralph/fleet.sh release "$N"`) so
the slot refills on the next wake; a `pr_opened` worker leaves its worktree in
Gate 3/4.

### Step 5 — Arm per-lane wakes (platform-aware), then end the turn

You want a wake the moment **any single lane** changes — not a barrier that waits
for all of them. **Background workers** already wake you on their own completion
— nothing to arm for a lane that's still building. For lanes in Gate 3/4 (an
open PR waiting on CI or the verdict), what you arm depends on which platform
this session runs on. Detect it once per wake: the
`mcp__github__subscribe_pr_activity` tool **exists** in a remote/mobile Claude
Code session and **does not exist** in a local terminal session.

**REMOTE (webhook-capable) session:**

1. **Per-PR webhook subscriptions** for every in-flight PR, so any one PR's CI
   failure or new review verdict wakes you independently:
   ```
   mcp__github__subscribe_pr_activity  (owner, repo, pullNumber)   # once per open PR
   ```
   Comment and CI-failure events arrive as `<github-webhook-activity>` and wake
   this session; a verdict comment wakes you directly. Webhooks never deliver CI
   *success* — but this repo's `iteration-trigger.yml` converts a fully-green CI
   + posted verdict into an owner-authored PR **comment**, which IS delivered, so
   even the green transition usually arrives as a webhook here.
   `subscribe_pr_activity` is **idempotent** — re-subscribing an already-watched
   PR every wake is safe and does not stack subscriptions, so just (re)subscribe
   every open PR each wake. Unsubscribe a PR once it merges/closes.
2. **ADAPTIVE `ScheduleWakeup` fallback** — pick the interval from the pool's
   shape this wake:
   - **Any lane has an open PR in CI/review** (`pending`/`awaiting-review`, or
     freshly pushed): arm a **short** fallback, ~180s. Webhooks drop, deliver no
     `behind→ready` transition or merge, and a sibling's merge silently pushes
     every other lane's `behind_by` above `0` with no event on those lanes' PRs
     — the short fallback bounds all of those blind spots to ~3 minutes.
   - **Every lane is still building (or the pool is empty)**: nothing is
     mid-flight for a webhook or a poll to catch — keep the long fallback,
     ~1200–1800s, as the safety net.

**LOCAL (terminal) session — no webhook MCP exists:**

1. **Launch a hot watcher per in-flight PR as a background task, every wake:**
   ```bash
   scripts/ralph/watch-pr.sh "$PR_NUM"        # Bash tool, run_in_background: true
   ```
   A background task's exit re-invokes this session, and the watcher exits —
   printing `WATCH <PR> <token>` — the moment `pr-ready.sh`'s token leaves the
   in-flight set (`pending`/`awaiting-review`/`main-not-green`/
   `review-quota-exhausted`/`ci-unreadable`) (or `gone` when the PR
   merges/closes, or `timeout <last-token>` at ~30 min). **The watcher's exit
   IS the per-lane wake** — seconds after the verdict or CI settles, instead
   of the full fallback sleep. Its pidfile (`/tmp/ralph-watch-<repo>-<PR>.pid`)
   makes relaunching **idempotent**: a duplicate exits immediately with
   `already-watching`, so just launch one for every in-flight PR each wake
   without bookkeeping.

   **`timeout ci-unreadable` — the persistent-stall escalation (#1408 §6):** a
   rollup that stays unreadable for the full ~30-minute window is a TOOLING
   fault, not a code fault — the tally probe itself (`gh pr checks`) has been
   failing or unparseable on every poll, not the tree. Re-classify once by
   hand: `gh pr view "$PR_NUM" --json statusCheckRollup`. If that read is
   clean, just relaunch the watcher — the prior run's unreadability was
   transient. If it is still unreadable, this is tooling weather: report it
   (or file an issue if it recurs) and leave the lane; do not burn a retry
   loop on it. **NEVER convert a `timeout ci-unreadable` into a
   `ci-debugging` dispatch** — nothing about the timeout proves the tree is
   red, only that the classifier could not tell either way, and dispatching a
   fix worker on that basis is precisely the bug #1408 closes.
2. **`ScheduleWakeup` long fallback** (~1200–1800s) stays armed as the safety
   net — it covers a killed watcher, a reboot, and anything else that slips
   past the pidfiles.

On **either** platform: **never foreground-block on a lane** — no foreground
`sleep`, no foreground `watch-pr.sh`, no waiting on a subscription in-turn.

Then **end the turn.** Do not run a Monitor that waits for all lanes to be
terminal — that is the barrier this design removes. Each independent wake re-runs
Step 0 and merges/refills whatever is ready.

---

## Worked example (why the slow lane never gates the fast one)

Pool of 4: issues A, B, C, D building in parallel. B is a tiny fix, D is a large
feature.
1. B finishes Gate 2.5, opens its PR; CI + review pass and `pr-ready.sh` reports
   `ready` (`LGTM`+green+`CLEAN`+`behind_by == 0`).
2. A wake fires (B's verdict). Step 1 merges **B now** — A, C, D are untouched and
   still mid-gate. Step 4 sees a free slot and assigns **E**, launching its worker.
3. D is still at Gate 1. It never blocked B, and B's merge didn't wait for D.
4. C later goes `LGTM`+green but `pr-ready.sh` reports `behind`
   (`mergeStateStatus BEHIND`, or `CLEAN` with `behind_by > 0` — either way, B
   and E landed since C went green). Step 1 syncs C; CI re-runs; C merges on
   the next wake once green. D keeps going the whole time.

Continuous throughput, four lanes always busy, merges strictly serialized and
always up-to-date.

## Sequential fallback

Set `parallel_enabled: false` (or `max_workers: 1`) in `state.json` and the pool
collapses to one lane: `fleet.sh free` reports at most 1, so Step 4 fills a single
slot and the loop behaves exactly like the classic one-issue-at-a-time Ralph —
still worktree-isolated, same gates, same drop-backs.

## Hard rules (do not deviate)
- **Merge a lane only when `scripts/ralph/pr-ready.sh` prints `ready`.** No
  other evidence merges a lane. "Up to date with `main`" means the compare
  API's `behind_by == 0` — **never** `mergeStateStatus` alone: this repo does
  not enforce strict/up-to-date status checks, so GitHub only computes
  `BEHIND` in the narrow case it would already block the merge on its own, and
  `CLEAN` routinely reports on a PR that is dozens of commits stale. A
  `behind` lane syncs first.
- **Never sync a lane while `main` is red — the fix lane is the one exception.**
  A sync imports the breakage, wastes a CI round, and re-reports as that lane's
  own `ci-failed`. `pr-ready.sh` says so mechanically by printing
  `main-not-green` instead of `behind` (#1159).
- **Never `fleet.sh release` a `review-quota-exhausted` lane** (#1160).
  `cmd_release` deletes the local branch, and a later `assign` recreates it
  from `origin/main`, orphaning the PR's already-pushed commits (issue #1180).
  `review-quota-exhausted` is the first token that can park a lane with an open
  PR for **days** — exactly the slot pressure that tempts a release. Leave it
  occupied; it un-wedges on its own (see the `review-quota-exhausted` bullet in
  Step 1).
- **Never make a fast lane wait on a slow one.** No per-tick barrier, no
  all-lanes Monitor. Act on whichever lane a wake is about; refill freed slots
  immediately.
- **Workers are background; never await them.** `run_in_background: true`, launch
  and end the turn.
- **Never more than `max_workers` worktrees.** `fleet.sh` enforces the cap; do
  not bypass it. **One issue per worker; one worker per worktree.**
- **Never track these issues with the Task tools.** (User directive.)
- **Never write to `main` directly** except `scripts/ralph/state.json`.
- **Never force-push.** Integration is `fleet.sh sync` (a merge), never a rebase
  of a pushed branch.
- **Never disable a CI check / pre-commit hook / lower a threshold.** Fix the
  root cause. If a tool is missing for an environmental reason, install it.
- **Re-entrancy first.** Read `state.json`, `fleet.sh list`, and PR state at the
  top of every wake; derive pool state from live git + GitHub, never from memory.
- **On merge, mark the issue done** (Step 1) and bump `state.json`.

## Anti-bypass (verbatim, non-negotiable)
> No bypasses. Do not add `# noqa`, `# type: ignore`, `# pylint: disable`,
> `@pytest.mark.skip`, or
> `git commit --no-verify`; do not lower coverage / branch / complexity /
> docstring thresholds in `pyproject.toml` or the scripts; do
> not delete tests or code to make a metric pass; do not swallow exceptions to
> silence a linter. Fix the root cause. The only allowed escape hatch is an
> inline `# noqa: RULE  # Issue #N: <reason>` (or `# type: ignore  # Issue #N:
> …`) tied to a real tracking issue, per `max-quality-no-shortcuts`.
