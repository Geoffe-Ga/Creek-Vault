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

## Two run modes (the coverage set changes; nothing else does)

1. **Full audit** (default): the coverage set is the whole codebase, as
   enumerated by the evidence bundle's `area-inventory.txt`.
2. **Targeted area scan**: the caller supplies a scope — an area id plus a
   path list (the De-Slop workflow's matrix does this from the registry in
   `.github/deslop-areas.json`). The scope **replaces** `area-inventory.txt`
   as the coverage set: read EVERY file in the scope plus the tests that
   cover it, against all 13 families, and the coverage ledger must prove
   full-scope coverage. Reading *outside* the scope is allowed — encouraged —
   for corroboration (duplication of scoped code elsewhere, cross-boundary
   layering violations), but **file only findings anchored inside the
   scope**; out-of-scope slop belongs to that area's own scan. Targeted
   scans never file a `report` issue — the job summary is the record.

Every other rule (two signals, guard list, dedup, templates, precision over
recall) is identical in both modes. Wherever the steps below say
"whole codebase", a targeted scan reads "whole scope".

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
creek_mcp`, `crawdad/crawdad`); the grep heuristics; git churn; and an
`area-inventory.txt` enumerating every area under the audited roots (the
authoritative coverage set for a full audit's Step 4 reading pass) — capturing
each into `$EVID`. It never modifies files and never aborts on a tool error.
Read `$EVID/README.txt`, then work through every output file. Treat
`area-inventory.txt` as the coverage set the Step 4 reading pass must cover;
churn.txt and reading-targets.txt only order where to start. Supplement with
targeted `Grep`/`Read` as candidates emerge.

### Step 3 — Triage candidates against the guard list

For each candidate from the evidence bundle, **first discard** anything in the
NOT-slop list (`slop-taxonomy.md`): generated code, migrations, lockfiles,
justified suppressions (with a linked issue), framework
boilerplate (Pydantic models, Typer command signatures, discord.py
listeners), test fixtures, self-evident
constants, deliberate repo conventions, and unmeasured "could be faster" claims.

### Step 4 — The reading pass (fan-out) — DO NOT SKIP

This is the step that finds the slop linters cannot see, and the step the first
audit skipped. Do not conclude "clean" without it.

**This is an EXHAUSTIVE reading pass over the ENTIRE coverage set on EVERY
run.** In a full audit the coverage set is the evidence bundle's
`area-inventory.txt`, which enumerates every area in the repo; in a targeted
scan it is the caller-supplied scope (fan out per module within it).
Fan out with the **Task tool** and spawn a reader subagent for **every** area
in the coverage set — never just the changed ones. For a full audit:

- one subagent per creek pipeline stage (`creek-tools/creek/{ingest,classify,
  link,author,generate,...}/*`) — these are the real feature areas,
- one for the MCP server surface (`creek-tools/creek_mcp/*`),
- one for the crawdad Discord bot (`crawdad/crawdad/*`, incl. `llm/` and
  `builtin_workflows/`),
- one for the shared/util/config grab-bags (prime duplication + dead-code sites),
- one (or more) for **every remaining area** — the top-level modules, the
  `scripts/` shell tooling, and the small, stable, low-churn modules — so
  nothing hides simply because it is small or hasn't changed in 90 days.

A clean linter bundle and an unchanged file are **NOT** reasons to skip the
reading pass for any area. **"Delta-focused", "since the last run", and "building
on last week's baseline" scoping are FORBIDDEN** — slop in older, stable code
must be read too. `churn.txt` / `reading-targets.txt` decide only the **order**
areas are read first, **never** which areas are skipped (files untouched in 90
days are absent from those lists by design).

Give each subagent the **full 13-family taxonomy** (`slop-taxonomy.md`) and this
brief: *read the actual source in your area and return corroborated candidates
for every family, with file:line evidence — focus on what linters miss: dead/
stubbed/orphaned code, duplication (here and against the rest of the repo),
architecture/layering violations, lying flags, verbosity, comment slop, AI-slop
tells, and weak tests. Ignore anything ruff/mypy/radon/bandit already gates.*

Use churn / largest-file lists only to prioritize the order. Collect all
subagent candidates before corroborating.

### Step 5 — Corroborate each survivor (the gate)

Apply the **Two-Signal Rule** (`detection-playbook.md`): no finding survives
without **two independent signals, at least one concrete and reproducible.**

- For any **correctness/bug** claim (taxonomy Family 0, and Family 12 "confident
  wrongness"): **write and run a reproducing test** in a throwaway location. If
  it does not reproduce, **drop it.** Never file a bug on reasoning alone.
- For dead code: a tool hit (vulture) **and** a grep proving zero inbound
  references *including* dynamic dispatch, Typer CLI commands, FastMCP tool
  handlers, discord.py listeners, and pytest fixtures. vulture alone is not
  enough — it false-positives on dynamic use.
- For everything else: tool/static signal + structural proof or a reading that
  explains the defect in terms of intent.

Classify each survivor by family and assign a severity (Critical/High/Medium/
Low) from the rubric. When corroboration is shaky, **downgrade or drop.**

For **Family-3 (dead/orphaned/stubbed) survivors**, after corroboration classify
the **remediation direction** — `delete` / `wire-in (+ e2e test)` /
`decision-needed` — using the intent + completeness signals from
`slop-taxonomy.md`. Wire-in requires **both** signals; the filed issue must then
demand an e2e test proving the newly wired path works end to end. This does not
loosen the two-signal corroboration gate above — it only decides what to
recommend *after* a finding has already survived it.

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

Then add a **coverage ledger** that proves FULL-COVERAGE-SET coverage two ways:
1. one row per taxonomy family (all 13) with the verdict (clean / candidates /
   filed), and
2. **every area in the coverage set (`area-inventory.txt` for a full audit;
   the caller's scope for a targeted scan) marked READ this run** — no area may
   be "unchanged → not read". This is how a reader verifies the whole taxonomy
   AND the whole inventory were actually traversed, not assumed clean:

```
| Family | Areas examined | Verdict |
|--------|----------------|---------|
| 0 Correctness | creek/ingest/* (all), creek/link/eddies.py | clean |
| 3 Dispensables | creek_mcp/* (all), crawdad/builtin_workflows | 2 filed (#812, #813) |
| ... | ... | ... |

Inventory coverage: 14/14 creek pipeline stages · creek_mcp · crawdad (incl.
llm/, builtin_workflows/) · shared/util/config · 3/3 scripts dirs — all READ
this run.
```

If nothing met the bar, say so plainly — *"Entire codebase read this run; clean
against the taxonomy"* (never *"delta since #N"*) — but the ledger must still show
all 13 families AND the full inventory were each read. A clean verdict with an
empty or inventory-incomplete ledger means the reading pass was skipped or
narrowed to changed areas; that is a failed run, not a clean one.

## What this skill must never do

- File a finding it could not corroborate with two independent signals.
- File a correctness/bug claim without a reproducing test.
- Implement fixes itself — it only files work; Ralph/`continue-epic` implements.
- Modify tracked files, weaken a quality gate, or commit anything.
- Touch generated code, migrations, lockfiles, or justified suppressions.
- Create duplicate issues, or flood the backlog with low-value nits.
- File a stylistic opinion that contradicts CLAUDE.md/AGENTS.md conventions.
- Recommend deleting orphaned/dead code without first checking the wire-in
  signals (intent + completeness) — reflexively discarding finished, intended
  work is itself slop.

## Examples

### Example 1 — Scheduled targeted scan (the primary path)

The `De-Slop` GitHub Action (`.github/workflows/deslop.yml`) fires — weekly on
the Monday cron, or dispatched by the Ralph loop's de-slop gate — running one
matrix job per area in `.github/deslop-areas.json` (or the user says "run the
slop detector", which is a full audit). Each job: collect evidence → triage →
**fan-out reading pass over its scope** → corroborate →
cluster → dedup → file → report-with-ledger. vulture + grep prove
`legacy_resonance()` has zero callers → file one `dead-code`/`priority-medium`
issue. A reading subagent finds crawdad's `router.py` mixes message parsing,
LLM dispatch, formatting, and 4 unrelated responsibilities (something radon's
complexity grade does *not* describe) → file a `refactor` **epic** with
sub-issues (via
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
existing `prompts/github-issues/audit-destub-05b-*.md` precedent (adapt the
naming convention to this project's own roadmap docs, if any).

### Example 4 — Orphaned-but-finished code (wire-in, not delete)

grep on `export_report()` in `creek-tools/creek/author/report_export.py`
shows a typed, fully implemented, unit-tested function with zero inbound
edges — no CLI command or MCP tool calls it (signal 1, the orphaned-code
tell). Reading `creek/cli.py` finds a docstring promising "export your vault
report as CSV" and a roadmap reference in `prompts/github-issues/README.md` to
the same feature (signal 2 = intent). Reading the function itself confirms it's
complete and matches the house pattern (typed, docstringed, follows the
existing pipeline-stage conventions) — not a husk (completeness). Both signals
hold, so this is **not** a deletion candidate: file a `de-slop`/`wire-in`
Template A issue that requires wiring the intended `creek` CLI subcommand to
the export function **and** adding an e2e test (Typer `CliRunner` driving the
real command) that proves the newly connected path returns the exported CSV —
not an issue to delete `report_export.py`.

## Troubleshooting

### A tool is missing in CI / locally
`collect-evidence.sh` skips absent tools and notes it. Install the dev deps
(`cd creek-tools && uv sync --all-extras`, or `pip install -e
"creek-tools/.[all,dev]"` inside a `.venv`) for the full toolbox; proceed with
whatever is available otherwise.

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
