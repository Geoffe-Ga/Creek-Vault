---
name: scan-issue-writer
description: >-
  Run a named codebase maintenance scan and convert findings into
  6-component, agent-ready GitHub issues. Use when a workflow or user says
  "run the <name> scan", "create issues from this analysis", or passes a
  prompts/scans/*.md file. Handles dedupe, priority labeling, and max-issue
  caps so the Ralph loop never starves and never bloats. Do NOT use for
  implementing fixes (use stay-green), reviewing PRs (use
  comprehensive-pr-review), or closing/triaging existing issues (use
  backlog-grooming).
metadata:
  author: Geoff
  version: 1.0.0
---

# Scan Issue Writer

Turn scan findings into prioritized, deduplicated, agent-ready GitHub issues
for the autonomous maintenance pipeline. The skill owns the *process*; each
`prompts/scans/<name>.md` owns the *domain* of its scan.

## Inputs

The invoking workflow (or user) supplies:

- **scan_name** — which scan to run; selects `prompts/scans/<scan_name>.md`.
- **max_issues** — hard cap on issues created this run (default 5).
- **priority** — `P0` | `P1` | `P2` | `P3`, applied to every issue filed.

## Instructions

### Step 1 — Load the scan definition
Read `prompts/scans/<scan_name>.md`. It defines the tools to run, what counts
as a finding, the severity rubric, and the title-slug prefix (`[scan:<name>]`).

### Step 2 — Run the analysis (read-only)
Execute the scan's tool commands. Never modify code. Record the current SHA
with `git rev-parse HEAD` — every issue must cite it in its Context section.

### Step 3 — Rank and cap
Sort findings by the scan's severity rubric, highest first. Keep at most
`max_issues`. Findings past the cut are NOT silently dropped: list them in a
single run-summary comment (Step 6) so the next scheduled or hopper-dispatched
run can pick them up. Never exceed the cap — the hopper controls volume.

### Step 4 — Dedupe before every create
For each surviving finding, derive its title slug and search open issues:

```bash
gh issue list --search 'in:title "<slug>"' --state open --json number,title
```

- **Exact match, finding still valid** → add a comment with fresh evidence
  ("Still present at `<SHA>`; <tool> confidence …"). Do not create a duplicate.
- **Exact match, evidence changed materially** → `gh issue edit` the body.
- **No match** → proceed to Step 5.

Duplicate issues inflate apparent queue depth with fake work — this step is the
single most important behavior for keeping the hopper honest. Other scans may
run concurrently, so re-run the dedupe search immediately before each create.

### Step 5 — Write the issue
Fill the canonical `prompts/templates/scan-issue-body.md` completely — all six
components, no placeholders left. (It is the single source of truth;
`references/issue-body-template.md` just points at it.) An issue missing any
component gets `needs-triage` instead of `agent-ready`, and the grooming pass
finishes it. Write the body to a file and create with:

```bash
gh issue create --title "[scan:<name>] <specific finding>" \
  --body-file /tmp/scan-issue.md \
  --label "<priority>,scan:<name>,agent-ready"
```

If any of the labels do not exist yet, create them first (see
`scripts/setup-scan-labels.sh` for the canonical set), then retry.

### Step 6 — Report
Append a run summary to `$GITHUB_STEP_SUMMARY` (or print it, when run locally):
findings found / created / deduped / capped-and-deferred, each with its issue
number. A clean scan reports zero created and that is a success.

## Examples

### Example 1 — Perf scan finds an N+1
Title: `[scan:perf] N+1 query loading marginalia in entries.list_for_user`
Labels: `P2,scan:perf,agent-ready`. Body cites the SQLAlchemy echo log,
`file:line` at the scanned SHA, and a `selectinload` before/after sketch.

### Example 2 — Dedupe hit
`[scan:dead-code] unused export parseAmount in lib/billing.ts` is already
open as #412 → add a comment: "Still present at `<SHA>`; vulture confidence
100%." No new issue is created.

## Troubleshooting

### Scan tool exits nonzero with no findings
Distinguish "tool crashed" from "clean scan." A genuine crash creates ONE `P1`
issue about the broken tooling (with the command and stderr). A clean scan
creates zero issues — that is success, not failure.

### More findings than max_issues
Never exceed the cap. Summarize the overflow in the Step 6 run summary so the
next run picks it up. Raising throughput is the hopper's job (higher
`max_issues`), not this skill's.
