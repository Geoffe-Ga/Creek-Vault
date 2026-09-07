# Wake protocol

Run this on **every** wake, in order. Each wake is stateless: reconstruct
everything from disk, `git`, and `gh`. Never assume continuity with the previous
wake — after a compaction there literally is none.

All paths are absolute. `R=$(git rev-parse --show-toplevel)`.

---

## Step 0 — Liveness triage (the supervisor hat)

This is the whole point of the cheap loop. Budget: **under a minute.**

```bash
cd "$R"
git worktree list                                   # lanes that exist
gh pr list --state open --json number,headRefName,mergeStateStatus,url
cat .ultracode/state.json 2>/dev/null
```

Classify the session in one of three states:

| State | Evidence | Action |
|---|---|---|
| **HEALTHY, nothing to do** | lanes exist, all mid-build or mid-CI, no free slot, no lane changed state since last wake | Re-arm wake. `noop: true`. **End the turn.** |
| **SOMETHING CHANGED** | a lane finished, a PR went green, a verdict landed, a slot is free, backlog stale | Put on the orchestrator hat → Steps 1–5 |
| **DEAD** | zero lanes AND zero open PRs AND backlog non-empty | The drain stopped. **Restart it** → Steps 3–5 |

The DEAD case is the one the operator asked the loop to catch. Do not
rationalise it ("maybe a workflow is still running") — if nothing is on disk,
nothing in `git worktree list`, and nothing in `gh pr list`, the drain is dead.
Restart it.

**Also reconcile drift**, cheaply, before deciding: a worktree whose PR merged,
a worktree whose branch no longer exists, a lane whose background task died. Any
of these frees a slot and moves you to SOMETHING CHANGED.

---

## Step 1 — Merge every ready lane (serialized, up-to-date only)

Merges happen one at a time; the single orchestrator session serializes them for
free. For each open PR, evaluate the §6 merge policy in SKILL.md.

Order of checks — cheapest first, and each one has failed a real merge:

```bash
PR=<n>
gh pr view "$PR" --json url,headRefOid,mergeStateStatus,statusCheckRollup
gh api "repos/{owner}/{repo}/compare/main...$BRANCH" --jq .behind_by   # must be 0
gh pr view "$PR" --comments --json comments \
  --jq '.comments[-5:] | .[] | {createdAt, author: .author.login, body: .body[0:200]}'
```

1. `behind_by == 0` — **not** `mergeStateStatus`, which says `CLEAN` on badly
   stale branches.
2. CI green on `headRefOid` — the *current* head, not "CI passed at some point".
3. Verdict `LGTM` **and** `createdAt` later than the head commit's timestamp.
4. Acceptance criteria met (check the dossier's checklist, not the issue body).

Then merge, close/annotate the issue, `release` the worktree, and bump
`completed_total` + `completed_since_groom` in `.ultracode/state.json`.

**Parse the verdict as an enum, never as a substring of prose.** The word
"wontfix" once appeared inside a rationale paragraph and silently changed the
outcome. Match the verdict line, tolerantly of format (`## Verdict` / `Verdict:`
/ a bare `✅ LGTM`), and read the token — not the body.

**Do not release a lane parked on a review-quota / rate-limit token.** Releasing
deletes the local branch; a later assign recreates it from `origin/main` and
orphans the PR's pushed commits. Leave it occupied; it un-wedges on its own.

---

## Step 2 — Advance failing lanes (per PR, independently)

For each lane that is red:

- **CI red** → `ci-debugging`. Reproduce locally first. Never label something
  "pre-existing" without diffing against pristine base in the same venv.
- **A CI failure whose steps are `null` after setup, with `BlobNotFound` logs and
  wall-clock wildly off** is runner eviction — infrastructure, not your code.
  Re-run; do not debug it.
- **Verdict `CHANGES_REQUESTED` / `COMMENTS`** → `address-feedback`.
- **Lane returned BLOCKED on a denied `git push`** → this is common and is not a
  lane failure. Finish the push from the orchestrator:
  `cd <worktree> && git push origin <branch>`. Orchestrator pushes succeed where
  lane pushes are denied. Note that the denial is **intermittent** — before
  opening a PR, check `gh pr list --head <branch>` or you will create a
  duplicate.
- **Lane ended its turn "waiting on" something** — a known premature stop. A
  `SendMessage` resume recovers it every time. Do not re-dispatch from scratch.

Never make a fast lane wait on a slow one. There is no per-wake barrier.

---

## Step 3 — Boundary gates (every Nth completion)

Read `completed_since_groom` against `groom_interval` (default 10):

- **At the grooming boundary:** run `session-retrospective` first (compaction
  destroys the evidence), then `backlog-grooming`, then `/compact`, then reset
  the counter and force a full backlog refresh.
- **At the quality-scan boundary** (a larger interval, default 30): run
  `de-slopify` to refill the backlog with corroborated findings. A drained
  backlog is not the end of the loop — it is the trigger for the scan that
  refills it.

---

## Step 4 — Refill every free slot, now

```bash
# up to max_lanes, honouring the RAM budget in SKILL.md §4
```

For each free slot: pick the highest-priority eligible issue (§3 of SKILL.md),
create its worktree off `origin/main`, and dispatch a **background** lane with
its dossier. If the wave has no dossiers yet, run Phase A (the premise+design
audit workflow) across the whole tranche **first** — one workflow for the wave,
not one per issue.

**Launch and move on. Never await a lane.** Its completion is its own wake.

If the picker returns nothing: the backlog is drained or nothing is compatible
with the current pool. That is a Step 3 quality-scan trigger, not a stop.

---

## Step 5 — Arm the wake, then end the turn

Two mechanisms, both armed:

1. **Per-lane hot signal.** Background lanes wake you on completion — nothing to
   arm. For lanes in CI/review, arm a per-PR watcher (a background task whose
   *exit* is the wake) or, in a webhook-capable session, a per-PR activity
   subscription. Subscriptions are idempotent; re-subscribe every open PR each
   wake rather than tracking which are already watched.
2. **`ScheduleWakeup` fallback, always.** 1200–1800s when lanes are building or
   watched; shorter only when you are genuinely polling external state nothing
   can notify you about. Pass the loop prompt **verbatim** so the next firing
   re-enters this skill.

Then **end the turn.** Do not run a monitor that waits for all lanes to be
terminal — that is the barrier this design exists to remove.

### The last check, every single turn

> **What will wake me, and can it actually fire?**

- "A monitor" → confirm its pattern matches every terminal state *including
  refusals and errors*, and that it does not exit while work remains.
- "A background task" → confirm the launch returned a task id **and** did not
  exit 127 on a stale relative path.
- "A workflow notification" → fine, but arm the fallback anyway; a workflow that
  dies silently takes the loop with it.
- No answer → **arm a wakeup before ending the turn.**
