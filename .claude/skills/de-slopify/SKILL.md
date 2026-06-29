---
name: de-slopify
description: >-
  Audit the codebase for sloppy, amateurish, or AI-generated low-quality code —
  bugs, code smells, dead/stubbed/orphaned code, duplication, poor architecture,
  needless verbosity, comment slop, type-safety erosion, and weak tests — then
  file corroborated findings as Ralph-ready GitHub issues (and decomposed epics
  for big work). Use when the user says "de-slop", "deslopify", "run the slop
  detector", "find code smells", "audit code quality", "find dead code", "weekly
  code quality scan", "bullshit detector", or when the weekly de-slop GitHub
  Action runs. Findings must be corroborated by two independent signals before
  filing — null findings are a valid result. Do NOT use for implementing fixes
  (Ralph / continue-epic does that), for reviewing a specific PR diff (use
  comprehensive-pr-review), for fixing CI failures (use ci-debugging), or for
  triaging a backlog of existing issues (use backlog-grooming).
metadata:
  author: Geoff
  version: 1.0.0
---

# De-Slopify — The Comprehensive Bullshit Detector

A reusable, scheduled code-quality auditor. It hunts the whole codebase for
**evidence** of slop — bugs, code smell, dead/stubbed/orphaned code,
duplication, poor architecture, verbosity, comment noise, type erosion, weak
tests, security candidates — **corroborates** each finding against two
independent signals, and files only the survivors as GitHub issues that the
local **Ralph** loop picks up autonomously. Big problems become **epics** with
decomposed sub-issues.

Its prime directive: **precision over recall.** A false positive wastes an
autonomous implementation cycle (or worse, "fixes" correct code into broken
code), so a finding that can't be corroborated is dropped. **A run that files
nothing because the code is clean is a success.**

## Two principles that decide whether a run is any good

1. **Linters are table stakes, not the audit.** This repo already passes ruff,
   mypy (strict), radon, xenon, bandit, and interrogate in pre-commit and CI on
   every commit. Anything those tools gate is *already enforced* and **cannot be a
   finding** — never report a complexity grade, a lint rule, or a type error as
   slop. The evidence bundle from `collect-evidence.sh` is dominated by exactly
   that output; treat it as a *map of where to look*, not as findings. Your
   entire value is the slop linters are **blind to**.
2. **The high-value families require reading code, not parsing tool JSON.**
   Dead/stubbed/orphaned code, duplication, bad architecture, lying feature
   flags, needless verbosity, comment slop, AI-generated tells, and weak/
   coverage-theater tests do not show up in a linter report. A 5-minute,
   single-threaded scan of tool output will conclude "clean" and miss all of
   them. **You must do the reading pass in Step 4.**

## The three references (read them — they are the substance)

| File | What it gives you |
|------|-------------------|
| `references/slop-taxonomy.md` | The exhaustive field guide: 13 families of slop (Correctness, Bloaters, OO-abusers, Change-preventers, Dispensables, Couplers/Architecture, Naming/Comments, Verbosity, Type-safety, Testing, Security, Deps/Config, **AI-slop-specific**), each with tells, corroboration hints, a severity rubric, and an explicit NOT-slop guard list. |
| `references/detection-playbook.md` | The Two-Signal Rule, the toolbox→category map, grep recipes, the weekly run procedure, and the precision calibration. |
| `references/issue-templates.md` | Standalone-finding and epic templates, the exact labels Ralph's picker honors, `gh` filing recipes, and the pre-file checklist. |

`scripts/collect-evidence.sh` runs the read-only toolbox and writes a complete
evidence bundle to the scratchpad.

## Instructions

### Step 1 — Orient

Read the house rules so findings respect intent, not a generic ideal:
`CLAUDE.md`, `AGENTS.md`, and skim `prompts/github-issues/README.md` for what's
already planned. Then read all three references above (at minimum the taxonomy
and playbook). Understand the NOT-slop guard list before you flag anything.

### Step 2 — Collect evidence (read-only)

```bash
EVID=$(bash .claude/skills/de-slopify/scripts/collect-evidence.sh | tail -1)
```

This runs ruff, vulture, radon, mypy, bandit, interrogate, pip-audit, and
detect-secrets over the Python source (`creek-tools/creek`, `creek-tools/
creek_mcp`, `crawdad/crawdad`); the grep heuristics; and git churn — capturing
each into `$EVID`. It never modifies files and never aborts on a tool error. Read
`$EVID/README.txt`, then work through every output file. Supplement with
targeted `Grep`/`Read` as candidates emerge.

### Step 3 — Triage candidates against the guard list

For each candidate from the evidence bundle, **first discard** anything in the
NOT-slop list (`slop-taxonomy.md`): generated code, Alembic migrations,
`package-lock.json`, justified suppressions (with a linked issue), framework
boilerplate (Pydantic/SQLModel/React Navigation), test fixtures, self-evident
constants, deliberate repo conventions, and unmeasured "could be faster" claims.

### Step 4 — The reading pass (fan-out) — DO NOT SKIP

This is the step that finds the slop linters cannot see, and the step the first
audit skipped. Do not conclude "clean" without it.

Fan out with the **Task tool**: spawn one subagent per feature area so the whole
codebase is actually read in parallel, not skimmed by one thread. A good split:

- one subagent per creek pipeline stage (`creek-tools/creek/{ingest,classify,
  link,author,generate,...}/*`) — these are the real feature areas,
- one for the MCP server surface (`creek-tools/creek_mcp/*`),
- one for the crawdad Discord bot (`crawdad/crawdad/*`, incl. `llm/` and
  `builtin_workflows/`),
- one for the shared/util/config grab-bags (prime duplication + dead-code sites).

Give each subagent the **full 13-family taxonomy** (`slop-taxonomy.md`) and this
brief: *read the actual source in your area and return corroborated candidates
for every family, with file:line evidence — focus on what linters miss: dead/
stubbed/orphaned code, duplication (here and against the rest of the repo),
architecture/layering violations, lying flags, verbosity, comment slop, AI-slop
tells, and weak tests. Ignore anything ruff/mypy/radon/eslint already gates.*

Use the `$EVID` bundle's churn and largest-file lists to prioritize. Collect all
subagent candidates before corroborating.

### Step 5 — Corroborate each survivor (the gate)

Apply the **Two-Signal Rule** (`detection-playbook.md`): no finding survives
without **two independent signals, at least one concrete and reproducible.**

- For any **correctness/bug** claim (taxonomy Family 0, and Family 12 "confident
  wrongness"): **write and run a reproducing test** in a throwaway location. If
  it does not reproduce, **drop it.** Never file a bug on reasoning alone.
- For dead code: a tool hit (vulture/eslint) **and** a grep proving zero inbound
  references *including* dynamic dispatch, FastAPI routes, DI, and pytest
  fixtures. vulture alone is not enough — it false-positives on dynamic use.
- For everything else: tool/static signal + structural proof or a reading that
  explains the defect in terms of intent.

Classify each survivor by family and assign a severity (Critical/High/Medium/
Low) from the rubric. When corroboration is shaky, **downgrade or drop.**

### Step 6 — Cluster, then dedup against the backlog

Group corroborated findings by area/theme. A cluster needing coordinated
multi-file change is an **epic**; standalone findings are single issues. Bundle
many Low findings in one file into a single tidy-up issue — don't flood the
backlog.

Then, for every cluster/finding, search existing issues and skip duplicates:

```bash
gh issue list --state open --search "in:title <keywords>" --json number,title
gh search issues --repo geoffe-ga/creek-vault "<keywords>" --state open
```

### Step 7 — File the work (Ralph-ready)

Follow `references/issue-templates.md` exactly. The labels matter — Ralph's
picker (`scripts/ralph/pick-next.sh`) skips `epic`/`blocked`/`needs-spec` and
works everything else lowest-number-first.

- **Standalone finding** → Template A, normal labels (no excluded label), sized
  ≤ ~300 net LoC / ≤ 5 files. Paste the corroborating evidence into the body.
- **Epic** → Template B with the `epic` label (Ralph skips the umbrella), plus
  sub-issues via Template A in dependency order. If the decomposition needs more
  than ~3 sub-issues, **invoke the `triage-and-plan` skill** to do it properly —
  don't hand-roll a sprawling epic. Mark dependent sub-issues `blocked` and note
  `Depends on #N`.

Create any missing labels first (`gh label create ...`). Write issue bodies to
files in the scratchpad and file with `--body-file` (never inline strings).

### Step 8 — Report (with a coverage ledger)

Emit a concise run summary: counts by severity, what was filed (with issue
numbers/links), what was **dropped and why** (failed corroboration / guard
list), and what was **deduped**.

Then add a **coverage ledger** — a table with one row per taxonomy family (all
13) recording which areas/files you examined for it and the verdict
(clean / candidates / filed). This is how a reader verifies the whole taxonomy
was actually traversed in the reading pass, not assumed clean:

```
| Family | Areas examined | Verdict |
|--------|----------------|---------|
| 0 Correctness | routers/*, domain/energy.py | clean |
| 3 Dispensables | services/, frontend/features/Habits | 2 filed (#812, #813) |
| ... | ... | ... |
```

If nothing met the bar, say so plainly — *"No corroborated slop this run —
codebase is clean against the taxonomy"* — but the ledger must still show the
13 families were each looked at. A clean verdict with an empty ledger means the
reading pass was skipped; that is a failed run, not a clean one.

## What this skill must never do

- File a finding it could not corroborate with two independent signals.
- File a correctness/bug claim without a reproducing test.
- Implement fixes itself — it only files work; Ralph/`continue-epic` implements.
- Modify tracked files, weaken a quality gate, or commit anything.
- Touch generated code, migrations, lockfiles, or justified suppressions.
- Create duplicate issues, or flood the backlog with low-value nits.
- File a stylistic opinion that contradicts CLAUDE.md/AGENTS.md conventions.

## Examples

### Example 1 — Weekly scheduled run (the primary path)

The `weekly-deslop` GitHub Action fires (or the user says "run the slop
detector"). Collect evidence → triage → **fan-out reading pass** → corroborate →
cluster → dedup → file → report-with-ledger. vulture + grep prove
`legacy_streak()` has zero callers → file one `dead-code`/`priority-medium`
issue. A reading subagent finds `HabitsScreen` mixes data-fetching, formatting,
and 4 unrelated render responsibilities (something radon's complexity grade does
*not* describe) → file a `refactor` **epic** with sub-issues (via
`triage-and-plan`). A suspected N+1 can't be confirmed without a query-count
log → **dropped** and noted in the report. Net: 1 issue, 1 epic (5 sub-issues),
3 dropped, 2 deduped — plus a 13-row coverage ledger in the job summary.

### Example 2 — Clean run

Evidence bundle yields only candidates that fail the guard list (a `# noqa`
with a linked issue, framework boilerplate, deliberate test-fixture
duplication). Nothing corroborates into a real finding. Report:
*"No corroborated slop this run."* File nothing.

### Example 3 — A "lying" feature flag (high-value find)

grep finds an `ENCRYPTION_AT_REST_ENABLED` flag and a docstring promising
encryption (signal 1); reading the model layer shows the column is written as
plaintext with no encrypt hook (signal 2). Corroborated and high-severity — the
code advertises a guarantee it doesn't deliver. File an `audit-destub`-style
epic to make it real (encrypt-on-write, key rotation, migration), mirroring the
existing `prompts/github-issues/audit-destub-05b-*.md` precedent.

## Troubleshooting

### A tool is missing in CI / locally
`collect-evidence.sh` skips absent tools and notes it. Install the dev deps
(`pip install -e "creek-tools/.[all,dev]"`, ideally inside a `.venv`) for the
full toolbox; proceed with whatever is available otherwise.

### Worried about false positives
Re-read the Two-Signal Rule and the NOT-slop guard list. When unsure, drop the
finding. Precision is the whole point — a missed smell costs nothing this week;
a bogus issue costs an autonomous implementation cycle.

### The backlog is filling with too many issues
Bundle Low-severity findings per file/area into single tidy-up issues, raise the
corroboration bar, and prefer a few ironclad high-value issues over many maybes.

### Ralph isn't picking up a filed issue
Check labels against `scripts/ralph/pick-next.sh`: an excluded label
(`epic`/`blocked`/`needs-spec`/etc.) or an open PR already referencing it will
make the picker skip it. Epics are skipped by design; their sub-issues are not.
