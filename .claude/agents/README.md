# Ralph subagent taxonomy

The cast of subagents the **Ralph loop** dispatches to build one backlog issue per
tick. The conductor is the main Ralph session
(`.claude/commands/ralph-tick.md` → `scripts/ralph/PROMPT.md`); it spawns every
agent below via the `Agent` tool. The chief-architect is the strategic **brain**
(plans + a dispatch list); the specialists are the **hands**.

> Shared rules — the four gates, quality thresholds, and the anti-bypass block —
> live once in [`shared/house-rules.md`](shared/house-rules.md).
> Every agent links there; change a rule once, there.

## The graph (honest — every node exists in this repo)

In the **parallel fleet** (`scripts/ralph/FLEET.md`), `ralph-tick` is the fleet
**orchestrator**: it manages up to `max_workers` (default 4) git worktrees and
dispatches one **`ralph-worker`** per issue, each of which is the per-issue
conductor *inside its own worktree* (it spawns the graph below). In the classic
sequential loop the orchestrator drives a single worker. Either way the spawn
graph under a conductor is identical:

```
ralph-tick (fleet ORCHESTRATOR — worker pool: reconcile · serialized-merge · lazy-sync · refill)
  └─ ralph-worker × up to 4 . L1  opus    per-issue CONDUCTOR in an isolated worktree
       ├─ chief-architect ..... L0  fable   plan + ordered dispatch list (no code)
       ├─ test-specialist ..... L2  opus    Gate 1 RED: failing tests          ─┐
       ├─ implementation-spec.  L2  opus    Gate 1 GREEN + Refactor             │ run per
       ├─ security-specialist . L2  opus    harden auth/input/paths/secrets     │ the
       ├─ performance-spec. ... L2  opus    profile/optimize hot paths          │ architect's
       ├─ documentation-spec. . L2  sonnet  docstrings/READMEs/ADRs             │ dispatch
       ├─ dependency-review ... L2  haiku   deps/pins/licenses (read-only)      │ list
       └─ code-review-orch. ... L1  sonnet  Gate 2.5 pre-push self-review      ─┘
```

**The tree above is the spawn graph: each conductor (`ralph-worker`, or
`ralph-tick` itself in the sequential loop) spawns every node under it directly.**
It is *not* a delegation hierarchy — the indentation does not mean chief-architect
spawns the others. chief-architect only *plans*; the conductor executes its
ordered dispatch list by spawning each specialist itself. The orchestrator
(`ralph-tick`) spawns `ralph-worker`s and nothing else; each worker spawns the
taxonomy inside its own worktree.

The frontmatter `delegates_to` / `receives_from` fields model **logical dataflow**
(who informs whom — e.g. the architect's risk flags reach the reviewers), **not**
the spawn mechanism, which is always the conductor. Only the two orchestrators
(chief-architect, code-review-orchestrator) hold the `Task` tool; the six
specialists are leaf workers that do their own work and do not sub-delegate.

> **Frontmatter caveat.** The Claude Code runtime only reads `name`,
> `description`, `tools`, and `model`. The extra fields here — `level`, `phase`,
> `delegates_to`, `receives_from` — are **descriptive documentation only**; they
> do not drive dispatch, ordering, or permissions. The conductor's dispatch
> logic is authoritative. Nested spawning (an orchestrator using `Task`) is
> best-effort: agents that rely on it must degrade to Read/Grep/Glob when it is
> unavailable, never stall.

> **Shared constraints are not auto-injected.** A subagent's context is only its
> own `.md` file; markdown links are inert. Every agent's Step 0 therefore
> **`Read`s** [`shared/house-rules.md`](shared/house-rules.md)
> at the start of its run so the gates, thresholds, and anti-bypass block
> actually bind — the link alone does not carry them into context.

## Model tiers (strategic mix)

The tiering rule is **Fable for planning, Opus 5 for implementation, Sonnet for
review, Haiku for quick checks.**

**Fable 5** for the one role where a better plan changes everything downstream:
`chief-architect` (the once-per-issue design pass). Fable is ~2× Opus-tier cost
and can run minutes-long turns — spending it once per issue on the design, and
not on every code-writing turn, is the cheapest place to buy quality. It also
prefers **less-prescriptive prompts** (state the goal and constraints, not
step-by-step scaffolding), which suits a planning brief.

**Fable is credit-metered and can run out.** The architect role therefore
carries an explicit **graceful fallback: on a Fable-unavailable spawn failure,
the conductor immediately re-spawns `chief-architect` with an
`model: opus` override and continues the tick.** A tick never blocks, and never
skips the planning pass, because Fable credits are exhausted. See
[`chief-architect.md`](chief-architect.md) and `scripts/ralph/PROMPT.md` step 5.

**Opus 5** for every code-writing role — implementation is where model quality
pays off directly: `ralph-worker` (the long-horizon per-issue conductor),
`implementation-specialist` (Gate 1 GREEN + Refactor), `test-specialist`
(Gate 1 RED), `performance-specialist` (optimization), and
`security-specialist`. Opus 5 is un-metered relative to Fable, so the roles that
run many turns per tick sit here. Keeping `security-specialist` on Opus is also
a hard requirement independent of cost: Fable's safety classifiers target
**cyber/bio** content, and legitimate hardening work can trip a false-positive
refusal.
**Sonnet** for review and other well-scoped passes over bounded input:
`code-review-orchestrator` (Gate 2.5), `documentation-specialist`.
**Haiku** for the purely mechanical, read-only checklist walk:
`dependency-review-specialist` (pins/lockfile/license checks need no deep
reasoning — spend the cheaper tier).

## Gate → agent invocation matrix

| Stage | Conductor action | Agent(s) |
| --- | --- | --- |
| Plan | architect the issue → plan + dispatch list | **chief-architect** |
| Gate 1 RED | write failing tests | **test-specialist** |
| Gate 1 GREEN | implement + refactor to green | **implementation-specialist** |
| Cross-cutting (only if flagged) | harden / optimize / document / vet deps | **security / performance / documentation / dependency** specialists |
| Gate 2 | run `cd creek-tools && ./scripts/check-all.sh` | — (conductor, Bash) |
| Gate 2.5 | pre-push self-review of the diff | **code-review-orchestrator** (reviews the flagged dimensions itself; may fan out to specialists in review mode where nested spawning is available) |
| Push / PR | commit, push, open PR | — (conductor) |
| Gate 3 fail (CI) | `ci-debugging` → fix | **test / implementation** specialist |
| Gate 4 fail (review) | `address-feedback` → fix | the specialist owning the comment's dimension |

## Dispatch sequence (one tick, one issue)

1. **chief-architect** reads the issue + `CLAUDE.md`/`creek-tools/CLAUDE.md`, returns an
   Architecture Plan with an ordered dispatch list and risk flags
   (security / performance / deps / docs).
2. The conductor executes the list **sequentially** (write-agents share one
   working tree — no parallel edits): test-specialist → implementation-specialist
   → any flagged cross-cutting specialists.
3. Conductor runs **Gate 2** (`check-all.sh`); failures drop to Gate 1 via the
   relevant specialist.
4. **code-review-orchestrator** runs **Gate 2.5** over the diff and returns a
   consolidated, severity-ranked report; the conductor fixes blockers (drop to
   Gate 1) until `CLEAN`.
5. Conductor commits, pushes, opens the PR; Gates 3–4 proceed per
   `ralph-tick.md`.

## Design rules

- **Omit, don't pad.** The architect names only the specialists a given issue
  needs; invoking an unneeded specialist is waste, not thoroughness.
- **Plans flow down, findings flow up.** chief-architect → specialists;
  code-review-orchestrator ← specialists.
- **No gate is ever weakened to pass.** Every drop-back is a root-cause,
  failing-test-first fix (see `shared/house-rules.md`).

## Files

| File | Agent `name:` |
| --- | --- |
| `ralph-worker.md` | ralph-worker |
| `chief-architect.md` | chief-architect |
| `test-specialist.md` | test-specialist |
| `implementation-specialist.md` | implementation-specialist |
| `security-specialist.md` | security-specialist |
| `performance-specialist.md` | performance-specialist |
| `documentation-specialist.md` | documentation-specialist |
| `dependency-review-specialist.md` | dependency-review-specialist |
| `code-review-orchestrator.md` | code-review-orchestrator |
| `shared/house-rules.md` | (shared reference) |
