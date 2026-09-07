# Portability — capability tiers

This skill is installed **user-level** and runs in any repo. Detect what the repo
gives you, then use it. Never assume one specific project's tooling.

```bash
R=$(git rev-parse --show-toplevel); cd "$R"
ls scripts/ralph/ 2>/dev/null            # Tier A fleet tooling?
ls .claude/commands/ .claude/agents/ 2>/dev/null
ls CLAUDE.md AGENTS.md */CLAUDE.md 2>/dev/null
ls scripts/check-all.sh */scripts/check-all.sh Makefile package.json 2>/dev/null
```

---

## Tier A — the repo already has a fleet harness

Signals: `scripts/ralph/` with `pick-next.sh`, `fleet.sh`, `pr-ready.sh`,
`watch-pr.sh`, `state.json`.

**Use it. Do not reimplement it**, and do not fight its conventions:

| Need | Use |
|---|---|
| pick next issue | `scripts/ralph/pick-next.sh` |
| create/list/release a lane | `scripts/ralph/fleet.sh assign\|list\|free\|release\|sync` |
| is a PR mergeable | `scripts/ralph/pr-ready.sh` — its `ready` token is the ONLY merge evidence |
| wake on a PR settling | `scripts/ralph/watch-pr.sh <PR>` in the background |
| lane cap, groom cadence | `scripts/ralph/state.json` |

Read the harness's own docs (`FLEET.md`, `PROMPT.md`, the `/ralph-tick` command)
before overriding anything. Two traps that recur:

- **Do not set the picker's exclude-labels env var** if it *replaces* rather than
  extends the default list — you silently re-admit `epic`, `blocked`, `wontfix`
  and `do-not-auto-merge`.
- Its configured `max_workers` is a **ceiling**, not a target; the RAM budget in
  SKILL.md §4 can lower it but never raise it.

In Tier A, `.ultracode/state.json` holds only what the harness does not: wave
number, dossier index, `last_backlog_refresh`.

---

## Tier B — no fleet harness (the common case)

Build the four primitives yourself. Keep them in-session; do not scaffold a
framework into someone's repo.

**Pick.** From `.ultracode/backlog.json` (SKILL.md §3), take the highest-priority
issue that is not in flight — no open PR whose body matches
`Closes|Fixes|Resolves #N`, no live worktree — and not excluded by label. Among
additional picks, prefer issues **independent** of every active one: no shared
epic label, no overlapping blast radius from the dossiers.

**Lane.**

```bash
git fetch origin
git worktree add ".ultracode/worktrees/issue-$N" -b "issue/$N-<slug>" origin/main
```

Worktrees, not branches-in-place: lanes must not share a working tree.

**Ready-to-merge.** Ask GitHub, in this order (see wake-protocol Step 1):
`behind_by == 0` from the compare API → CI green on the current `headRefOid` →
`LGTM` verdict postdating the head commit.

**Wake.** No `watch-pr.sh` here, so either arm a per-PR activity subscription (if
the session has one) or rely on background-task completion plus the
`ScheduleWakeup` fallback. Never foreground-poll.

**Release.**

```bash
git worktree remove ".ultracode/worktrees/issue-$N" --force
git worktree prune
```

---

## Repo conventions to read before the first lane

Re-read these every wave — waves are stateless and house rules change:

- `CLAUDE.md` / `AGENTS.md` at root **and** in the package subdirectory. Both are
  authoritative; the nested one usually carries the real quality thresholds.
- The quality gate's actual entrypoint: `scripts/check-all.sh`, `make check`,
  `npm run ci`, `just check`, `tox`. **Run the repo's script, never the
  underlying tool directly** — the script encodes flags you will otherwise miss.
- Commit conventions and pre-commit hooks. If direct commits to `main` are
  blocked, branch first — a resumed wake often starts on `main`.
- Whether the repo has a knowledge graph or code index; query it before doing
  grep sweeps, and fail soft to normal file tools if it is unavailable.

## Things that are NOT portable — verify, never assume

- Which reviewer identity posts the verdict, and in what format. Match it
  tolerantly (`## Verdict`, `Verdict:`, a bare `✅ LGTM`) or the loop stalls on a
  real verdict.
- The default branch name.
- Whether PRs come from forks. Check `isCrossRepository` before planning a push;
  pushing to a fork PR silently creates a duplicate branch on origin.
- Whether the repo has more than one dependency manifest. Version floors and CVE
  fixes are **per-manifest** — enumerate every lockfile.
- Red dependency-bot PRs are often **deliberate, test-enforced holds**, not
  neglect. Read why before batching them.
