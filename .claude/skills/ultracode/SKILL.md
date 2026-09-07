---
name: ultracode
description: >-
  Run an autonomous, multi-agent backlog drain that keeps going for days without
  being kicked. Takes the WHOLE open GitHub backlog as the standing goal, works
  it in priority waves, orchestrates every issue with the Workflow tool
  (premise-verify → panel design → build → adversarial review) rather than solo
  inline work, drives each issue through the TDD/quality gates via stay-green,
  answers the PR reviewer via address-feedback, merges autonomously on
  green + LGTM, and re-arms its own wake every single turn so the loop never
  dies silently. Use when the user says "ultracode", "drain the backlog",
  "work the backlog until it's empty", "run the fleet", "keep going on the
  issues", or invokes /loop over backlog work. Do NOT use for a single named
  issue (use stay-green, or the repo's own per-issue worker contract), for
  reviewing one PR (use comprehensive-pr-review), or for backlog triage with no
  intent to implement (use backlog-grooming).
metadata:
  author: Geoff
  version: 1.0.0
---

# Ultracode

An **autonomous backlog drain**. The operator is away. The backlog is the goal.
Your job is to keep merging correct, fully-verified work until the backlog is
empty — and, above all, **to still be running when they come back.**

> **The one failure that matters:** if the operator has to type "continue", the
> loop was dead and you did not notice. That is your defect, never their
> prompting. Every rule below exists to prevent it.

---

## 0. The two hats

This skill is one session wearing two hats. Do not confuse them.

| Hat | Fires when | Costs | Does |
|---|---|---|---|
| **Supervisor** | every `/loop` wake | seconds | Liveness triage. Is work in flight and healthy? If yes → re-arm, `noop: true`, end turn. |
| **Orchestrator** | only when triage says something needs doing | minutes | Merge ready lanes, refill free slots, run the Workflow phases, refresh the backlog. |

The overwhelming majority of wakes are **supervisor** wakes and must end in
under a minute. Doing full orchestration on every wake is how a loop starves
itself. Step 0 of the wake protocol decides which hat you are wearing.

---

## 1. Preflight — once, on first invocation

Run these before anything else. They are cheap and every one of them has burned
a real run.

```bash
REPO_ROOT=$(git rev-parse --show-toplevel)   # use ABSOLUTE paths forever after
cd "$REPO_ROOT"
gh auth status                                # exit != 0 → stop, tell the operator
gh repo view --json nameWithOwner --jq .nameWithOwner
```

1. **Detect the capability tier** — whether this repo already has fleet tooling
   or you must use the portable fallbacks. See `references/portability.md`.
2. **Compute the lane budget.** RAM, not agent count, is the binding constraint.
   See §4. Write the answer into `.ultracode/state.json` as `max_lanes`.
3. **Check what else is running on the box** — a sibling Claude session's fleet
   is invisible to your bookkeeping and contends for the same RAM:
   ```bash
   ps -eo pid,rss,pcpu,args | awk 'NR==1 || $2>200000' | sort -k2 -rn | head -20
   ```
4. **Confirm auto-compact is on** (`/config` → Auto-compact). The loop is
   designed to survive compaction (§8), but only if compaction actually happens
   instead of the session wedging at full context.
5. **Full backlog pull** (§3) and open wave 1.

Then go to the wake protocol.

---

## 2. The ultracode recipe — Workflow → lanes → Workflow

**This is the part people skip, and skipping it is the single most common way
this skill degrades into ordinary work.** Dispatching per-issue worker agents is
*not* ultracode. A worker is multi-agent internally, which is exactly what makes
the omission easy to miss — but it is one conductor, one design, one sequential
pass. That is the baseline this skill upgrades *from*.

The two layers are complementary. Run **both**, in this order, per wave:

| Layer | Tool | What only it can do |
|---|---|---|
| **Think** | `Workflow` | Parallel fan-out, premise refutation, competing designs judged against each other, adversarial verification |
| **Build** | `Agent` (worker/lane) | Own a worktree for hours: gate loops, pushes, PR, drop-backs |

### Phase A — Premise + design audit (Workflow, one run per WAVE, not per issue)

Batch the whole next tranche into a single workflow. Per issue, in parallel:

1. **Verify the premise at current HEAD.** Issues are routinely wrong about
   their own facts — in one measured wave, 4 of ~6 were. Line numbers in issue
   bodies drift 100–300 lines. Emit a structured
   `premises: [{claim, holds, location, evidence}]` array, and say explicitly in
   the prompt: *"a premise of this issue being false is a finding to report, not
   a problem to work around."*
2. **Re-derive the complete acceptance-criteria checklist.** Issue bodies
   consistently understate it; expect 15–25 real criteria where the body lists 5.
3. **Two or more independent designs under opposing lenses** (minimal-diff vs.
   structural), then a judge that must **refute both** before picking one.
4. **Write a dossier** to `.ultracode/dossiers/issue-<N>.md` naming the HEAD sha
   it was verified against, a CORRECTED SCOPE section that supersedes the issue
   body, the judged `final_plan`, and the exact RED test.

This phase pays for itself immediately: it catches already-fixed issues, wrong
file paths, and inverted premises that would each have burned a whole lane.

### Phase B — Build (background lanes)

Hand each lane its dossier. A lane with a dossier does **not** need its own
architect pass. Tell it explicitly:

> Premise, evidence, ACs and refutations are **authoritative**. The
> implementation plan is a **proposal to re-derive** against current source —
> every `file:line` may have shifted, re-read before editing. A premise of this
> brief being false is a finding to report, not a problem to work around.

Full template in `references/lane-brief.md`. Launch with
`run_in_background: true` and **never await a lane** — its completion is its own
wake.

### Phase C — Adversarial review (Workflow, at Gate 2.5 / on the wave's PRs)

Perspective-diverse review (correctness / security / reproduction), where **every
finding is verified by an independent skeptic before it counts**. Target what
author self-review structurally cannot catch: vacuous tests, deleted live
assertions, emptied `parametrize` lists, tests that only exercise the
non-default path.

Run this **from the orchestrator**, not from inside a lane — lanes frequently
run in sessions where the `Agent` tool is absent entirely, which silently
degrades their Gate 2.5 to author self-review. Read every lane's
`notes_for_operator` before merging; that is the only place a lane discloses it.

### Combining issues into one lane — do it deliberately

The operator explicitly wants related work merged into a single distributed
workload rather than N mechanical lanes. **Combine when** issues share a
chokepoint file or helper, are the same defect pattern (fix the *pattern*
repo-wide, not the reported sample), or one is a strict subset of another.
**Never combine** a contract/wire-shape change with an unrelated fix, issues in
different priority tiers, or anything labelled `solo`/`epic`/`blocked`. Cap a
combined lane at ~3 issues and require **every** issue's full AC checklist —
combining is an efficiency, never a scope reduction, and each combined issue
needs its own `Closes #N` line.

---

## 3. Backlog refresh — every wave, never from memory

**A backlog snapshot decays.** Refresh at the top of every wave (not every
wake). One full pull, partitioned locally:

```bash
gh issue list --state open --limit 500 \
  --json number,title,labels,updatedAt,url > .ultracode/backlog.json
```

- **Never slice with repeated `--label` flags.** GitHub ANDs them. A label-sliced
  fan-out once missed 146 of 304 issues.
- **Assert the partition covers the population**: the bucket counts must sum to
  the total before you fan out. If they do not, your filter is wrong.
- Rank by priority label tier, then issue number ascending (oldest first within
  a tier). Issues with no priority label default to the middle tier.
- **Exclude** from picking: anything already in flight (an open PR whose body
  says `Closes|Fixes|Resolves #N`, or a live worktree), plus
  `epic`/`blocked`/`wontfix`/`do-not-auto-merge`/`question` and any repo-local
  hold labels.
- Re-check holds each wave. A hold recorded weeks ago is often long since
  resolved; verify with `gh`, don't trust a remembered list.

---

## 4. The lane budget — RAM is the constraint

Agent count is not the budget. **Resident memory is.** A lane running a full
test suite holds ~1 GB *per test worker*, and `-n auto` gives every lane one
worker per core, so workers multiply instead of dividing.

```
lanes × workers_per_lane × ~1GB   must fit in AVAILABLE RAM
```

- **Hard cap: 4 concurrent lanes**, or `cores ÷ 2`, or the repo's configured
  `max_workers` — whichever is **lowest**. A configured cap is a ceiling, not a
  target.
- **Pin the per-lane test workers.** Export a slice (e.g. `PYTEST_WORKERS=2`,
  `CREEK_TEST_WORKERS=2`) so lanes do not each claim the whole machine.
- **Read available RAM correctly.** macOS: `vm_stat` → free + inactive +
  speculative. Linux: `free -m` `available` column. Under **~3 GB available,
  back off.** `memory_pressure` lies optimistically (counts reclaimable pages);
  `vm.swapusage` lies pessimistically (stale high-water mark that never moves
  after a fleet exits). Print both, but decide on available RAM. **A resource
  number that stays pinned rather than fluctuating is stale, not binding** —
  check whether anything is actually running before believing it.
- **Never run your own `check-all` / broad test sweep while lanes are live.**
  That is self-inflicted contention.
- **Prefer fewer, sequenced waves over one wide fan-out.** Wide is often not
  faster: 7 parallel lanes once returned 1 result in 2.5 hours while saturating
  the box.
- **Stopping a blocked fleet is free.** Lanes blocked on a shared defect burn
  RAM producing nothing — stop them, fix the shared cause solo, re-dispatch.

### Before fanning out, find the shared cause

When several *unrelated* lanes fail **identically on code they did not touch**,
the fault is upstream of all of them — a shared config, a hook environment, a
pinned dependency, the base branch. Diagnose **one** representative failure to
root cause *before* dispatching anyone. And **pilot one lane end-to-end**,
through the final push/merge step, before scaling: a fleet that cannot complete
its last step produces nothing, and its progress logs look identical to healthy
work.

---

## 5. Per-issue quality bar — non-negotiable

The bar is **every acceptance criterion**, not a reasonable subset.

- **TDD via `stay-green`.** Red → Green → Refactor. Gate 2 failing drops you
  back to Gate 1; you never weaken the gate.
- **Prove the test is non-vacuous by breaking the code.** Revert the fix,
  confirm the test fails with the signature you expect, restore, confirm green.
  Record that in the PR. A test that passes against unfixed code proves nothing.
- **Enumerate every instance before declaring a defect fixed.** The reported
  location is a sample, not the population. Grep for the *pattern*, fix all of
  them, and state in the PR which sites you swept — **as a table listing every
  candidate site including the safe ones and why they are safe**, not as a
  "found them all" assertion. Prefer one shared helper over N copies of the fix.
- **Distrust a gate that reports it did nothing.** "no files to check",
  "0 tests collected", "skipped", an instant pass — these look like success and
  are not. One mistyped test path collects zero tests and reads as a fast pass.
  Read the collected count; re-run explicitly against the files you changed.
- **Anti-bypass, verbatim:** no `# noqa`, `# type: ignore`,
  `# pylint: disable`, `@pytest.mark.skip`, `--no-verify`; do not lower any
  coverage / complexity / docstring threshold; do not delete tests or swallow
  exceptions to silence a linter. Fix the root cause. The only escape hatch is
  an inline `# noqa: RULE  # Issue #N: <reason>` tied to a real tracking issue.
- **Know the repo's pre-existing local failures** and put that named list in
  every lane brief with the sentence *"any other failure is yours."* Without it,
  every lane spends a cycle re-proving the same reds are not its own.

---

## 6. Merge policy

Merge autonomously — no per-PR confirmation — when **all** hold:

1. CI is green on the **current HEAD**;
2. the reviewer's verdict is `LGTM` and **postdates the head commit** (verdicts
   routinely land seconds before your next push — compare timestamps, always);
3. the branch is **not behind** `main` — measure with the compare API's
   `behind_by == 0`, **never** `mergeStateStatus` alone, which reports `CLEAN`
   on branches dozens of commits stale;
4. the issue's full acceptance criteria are met.

- **`COMMENTS` is not blocking.** File each actionable item as a labelled
  follow-up issue, then merge. Never hold a green PR for a nit.
- **`CHANGES_REQUESTED`** → back to Gate 1 via `address-feedback`.
- A **real** `behind` can still be mergeable when the file sets are disjoint —
  but a **false** `behind` from a stale local ref destroys an LGTM for nothing.
  `git fetch` and re-check against the API before syncing.
- **Never sync a lane holding a fresh LGTM** (the stale-verdict guard destroys
  it) and **never sync while `main` is red** — you import the breakage and
  re-report it as that lane's own failure.
- **Never force-push.** Integrate by merge, so a plain push updates the PR.
- **Never write to `main` directly**, and branch *before* editing — a resumed
  wake often starts on `main`.
- Watch out for prose: GitHub closes an issue on any `fixes #N` in a PR body,
  grammar be damned. Write deferred references as `re: #N`.

---

## 7. Wake discipline — the rule that keeps the loop alive

**Before ending any turn, answer out loud: what will wake me, and can it
actually fire?**

- A turn ending with **any** outstanding item — a PR in review, a workflow
  running, an unstarted backlog issue — must end with `ScheduleWakeup` carrying
  the loop prompt **verbatim**. Stopping is a decision that needs its own
  justification; continuing is the default.
- **A monitor that watches a finite set is not a loop.** It breaks when that set
  resolves, and then nothing can wake you. Either it never breaks, or you also
  arm `ScheduleWakeup`. Preferably both — the monitor for latency, the wakeup as
  the heartbeat.
- **Match the delay to what you are waiting for.** Lanes in CI/review with a
  live watcher: a long fallback (1200–1800s) is right; the watcher is the
  primary signal. Polling external state nothing can notify you about: size the
  delay to how fast that state actually changes. Never poll at 60s for work the
  harness will re-invoke you for anyway.
- **Every script invocation uses an absolute path.** The Bash tool's cwd
  persists across calls, so a `cd` into a worktree three calls ago silently
  turns a relative watcher path into exit 127 — and arming *looks* like it
  succeeded, because the tool cheerfully returns a background task id. **Read
  the exit status of every background launch.** 127 means the wake never armed.
- **Never foreground-block.** No foreground `sleep`, no waiting on a lane
  in-turn.
- Report a cost or context number once when it is material. **Never convert it
  into a question, and never stop because of it.**

---

## 8. Surviving compaction

The loop must outlive its own context. It does that by keeping **all** loop
state on disk and re-deriving from disk on every wake:

- `.ultracode/state.json` — wave number, lanes, `completed_total`,
  `completed_since_groom`, `groom_interval`, `last_backlog_refresh`, `max_lanes`.
- `.ultracode/dossiers/issue-<N>.md` — verified premise + judged plan per issue.
- `.ultracode/backlog.json` — the current wave's snapshot.
- Live truth always beats the file: `git worktree list`, `gh pr list`,
  `gh issue list`. **Derive pool state from git + GitHub, never from memory.**

Run the **`session-retrospective`** skill *before* each compaction — at the
grooming boundary or ~80% context, whichever comes first. Compaction destroys
the evidence of what went wrong; the retrospective converts it to memory first.
Then `/compact`.

After a compaction, the `/loop` prompt re-enters this skill with no memory of
the previous wake. That is fine, and it is the design: **be re-entrant, assume
nothing.**

---

## 9. Standing authority — decide, do not ask

Default: **decide, state the assumption in one line, proceed.** A reversible
decision made and flagged beats a correct decision made three hours later. Where
an issue offers a "suggested direction", that IS the default — take it and say so.

**Escalate only if at least one holds:**

1. It contradicts a **ratified decision record** — an ADR, a published contract,
   a documented policy.
2. It **spends money, provisions credentials, or touches an external account.**
3. It **changes a promise made to users** — privacy posture, data routing, a
   guarantee in product docs.
4. It is **irreversible AND unverifiable** beforehand.

Explicitly *not* qualifying: a destructive-*sounding* change that provably
destroys nothing; a design fork the issue labels "needs design"; a large diff;
a big cost number.

**The trigger is the act of typing a question at the end of a turn.** When you
are about to write "your call", "let me know", "should I", or "would you like" —
stop, apply the four tests, and if none hold, decide and keep going. A question
is only free if the operator is already waiting. They are not.

Two standing calls worth stating because they recur:

- **When a flag, banner, or doc claims a capability the code does not have,
  remove the claim** — do not build the missing feature to justify it.
- **Privacy/permission tiers escalate only.** If a change would make anything
  less restrictive for any subject, stop and report. If it is a strict narrowing
  at every ceiling, just do it.

---

## 10. Reference index

Read these on demand; do not preload them.

| File | Read it when |
|---|---|
| `references/wake-protocol.md` | Every wake. The Step 0–5 machine, verbatim. |
| `references/workflow-recipes.md` | Before writing any Workflow script. Includes the `${VAR}` escaping gate that kills runs at 0 agents. |
| `references/lane-brief.md` | Dispatching a lane. |
| `references/portability.md` | First run in a repo. Capability tiers + generic fallbacks. |
| `references/hazards.md` | A lane reports something strange, or a gate looks too green. |

## Related skills

`stay-green` (Gates 1–2) · `address-feedback` (Gate 4) · `ci-debugging` (Gate 3
failures) · `backlog-grooming` (the every-Nth-merge boundary) · `de-slopify`
(the quality scan boundary) · `session-retrospective` (before every compaction) ·
`max-quality-no-shortcuts` (anti-bypass) · `comprehensive-pr-review` (Phase C
review rubric).
