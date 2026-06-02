---
name: spec-decomposition
description: >-
  Decompose a SPEC, PRD, RFC, or design doc into a sequenced set of
  GitHub epics and child issues, where each issue is a self-contained
  prompt an agent or engineer can execute end-to-end. Use when the user
  says "decompose this spec", "break this PRD into issues", "file epics
  from this design doc", or "plan the implementation". Each issue is
  shaped as a 6-component prompt (prompt-engineering), sequenced via
  tracer-code so the skeleton lands first and every later issue keeps
  the system demoable, and gated by stay-green Done-Done plus
  max-quality-no-shortcuts anti-bypass rules. Issue bodies are written
  to `git-issues/` and filed via `gh issue create --body-file`, matching
  the file-based pattern in git-workflow. Do NOT use for single-task
  issue or PR creation (use git-workflow), backlog cleanup of existing
  issues (use backlog-grooming), PR code review (use
  comprehensive-pr-review), or choosing between technical approaches
  inside a SPEC (use architectural-decisions).
metadata:
  author: Geoff
  version: 1.0.0
---

# Spec Decomposition

Turn a SPEC into a sequenced set of GitHub epics and child issues. Each issue is a complete prompt — anyone (human or agent) can pick it up and execute it without needing to re-read the SPEC.

This skill stitches four other skills together:

- **prompt-engineering** — every issue body uses the 6-component prompt frame (Role / Goal / Context / Format / Examples / Constraints).
- **tracer-code** — issues are ordered so the skeleton lands first and every later issue preserves a working, demoable system.
- **stay-green** — every issue ends with explicit Done-Done gates: tests pass + pre-commit clean.
- **max-quality-no-shortcuts** — every issue forbids `noqa`, `type: ignore`, and similar bypasses except under the documented escape hatch.

Files land in `git-issues/` at the repo root and are filed with `gh issue create --body-file`, the same file-based pattern git-workflow uses.

## Instructions

### Step 1: Read the SPEC End-to-End

Read the entire SPEC before writing anything. Note:

- Goals, non-goals, and success criteria.
- External constraints (deadlines, dependencies, deploy targets).
- Open questions or under-specified areas — flag these to the user before decomposing; do not invent answers.

If material questions remain, **stop and ask the user**. A decomposition built on guesses produces a backlog of guesses.

### Step 2: Identify Epics (Workstreams)

Group the SPEC's surface area into 2-6 epics. An epic is a vertical workstream that:

- Delivers user-visible or system-visible value on its own.
- Has a single owner-shaped outcome ("Auth works", "Billing API responds with real numbers", not "Refactor utils").
- Can be sequenced relative to other epics.

If you find yourself with one giant epic, split it. If you have ten tiny epics, merge them — epics are coarse on purpose.

### Step 3: Sequence Issues Within Each Epic Using Tracer-Code

Inside each epic, the **first issue must be the skeleton issue** for that epic. Then real-feature issues replace stubs one at a time.

| Phase | Issue Type | Done-Done Looks Like |
|-------|-----------|----------------------|
| Skeleton | Wire all endpoints/UI/CLI surfaces with stubs returning typed mock data | Smoke tests prove every surface returns the right shape |
| Core | Replace P0 stubs with real logic | Feature works end-to-end with realistic input |
| Edges | Validation, error paths, edge cases | Negative tests pass; meaningful errors returned |
| Polish | Logging, metrics, docs, perf | No new features; tighten what exists |

If the skeleton can't be defined in one issue, the epic is too large — split it.

See `references/sequencing-patterns.md` for worked sequences (HTTP API, CLI tool, UI feature, data pipeline).

### Step 4: Write Each Issue as a 6-Component Prompt

Every issue body uses the prompt-engineering 6-component frame. The issue is not a description of work — it is a **prompt** an agent can execute:

1. **Role** — who should pick this up (e.g., "Senior Python engineer working in this FastAPI repo").
2. **Goal** — the specific, measurable outcome.
3. **Context** — file paths, prior decisions, links to the SPEC section, parent epic.
4. **Output Format** — what the deliverable looks like (code change + tests + docs update).
5. **Examples** — minimal before/after, expected request/response, or a test case.
6. **Constraints** — anti-bypass rules, scope fences, dependency ordering.

The full body template is in `references/templates.md`.

### Step 5: Embed Done-Done Gates (stay-green)

Every issue ends with an explicit Done-Done block. No issue ships without:

- [ ] All new and existing tests pass (`./scripts/test.sh --all` or repo equivalent).
- [ ] Pre-commit clean (`pre-commit run --all-files`).
- [ ] Coverage threshold met for the changed lines.
- [ ] PR opened with `Refs #<epic>` and `Closes #<this-issue>`.

If the repo runs the Claude reviewer GitHub Action, add: "Latest `Verdict:` on HEAD must be `LGTM`."

### Step 6: Forbid Bypasses (max-quality-no-shortcuts)

Every issue's Constraints section must include the anti-bypass clause:

> No `noqa`, `# type: ignore`, `pylint: disable`, `eslint-disable`, or equivalent. Fix the root cause. The only exception is the documented 4-line escape hatch (third-party bug / version compat / benchmarked perf / generated code) with a review date.

This is non-negotiable boilerplate; copy it verbatim from `references/templates.md`.

### Step 7: Write All Files to `git-issues/` First

Before calling `gh`, lay out the full decomposition on disk so the user can review it as a whole.

```bash
mkdir -p git-issues
```

Naming convention (uses file-naming-conventions ISO 8601 prefix for the batch):

```
git-issues/
├── 2026-05-15_SPEC_summary.md          # one-page restatement of the SPEC
├── EPIC_01_auth.md
├── EPIC_02_billing.md
├── EPIC_01_ISSUE_01_skeleton.md
├── EPIC_01_ISSUE_02_login-endpoint.md
├── EPIC_01_ISSUE_03_token-refresh.md
├── EPIC_02_ISSUE_01_skeleton.md
└── ...
```

`EPIC_NN` and `ISSUE_NN` are sequence numbers within the decomposition — they get replaced with real GitHub issue numbers after filing (Step 8). Keep the slugs short and stable so cross-references survive.

**Show the user the full file list and at least one epic body + one issue body before filing.** Filing is irreversible enough that a dry-run review saves hours.

### Step 8: File Epics First, Then Children with Parent Refs

Order matters: epics must exist before children can reference them.

```bash
# 1. Check for an existing label set; create only what's missing.
gh label list
gh label create "epic" --description "Tracks a workstream from a SPEC" --color "5319e7"
gh label create "spec-decomposition" --description "Issue filed from a SPEC decomposition" --color "0e8a16"

# 2. File each epic, capture its number.
EPIC_01=$(gh issue create \
  --title "epic: Auth" \
  --body-file git-issues/EPIC_01_auth.md \
  --label "epic,spec-decomposition" \
  --json number --jq .number)

# 3. Substitute the real epic number into every child body, then file.
sed -i "s/EPIC_01_NUMBER/$EPIC_01/g" git-issues/EPIC_01_ISSUE_*.md

gh issue create \
  --title "feat(auth): Wire skeleton routes with stubbed responses" \
  --body-file git-issues/EPIC_01_ISSUE_01_skeleton.md \
  --label "spec-decomposition,tracer-code"
```

After filing every child, edit each epic on GitHub (or via `gh issue edit --body-file` after rewriting the file) to add the child issue checklist:

```markdown
## Child Issues
- [ ] #123 — Skeleton routes
- [ ] #124 — Login endpoint
- [ ] #125 — Token refresh
```

### Step 9: Hand Off

When filing is done, post a single comment back to the user with:

- Total epics filed (with numbers).
- Total child issues filed.
- The recommended **first issue to start with** (always the skeleton of the highest-priority epic).
- A pointer to `git-issues/` for the canonical source-of-truth files (commit these — they're the trail of how the backlog was generated).

## Examples

### Example 1: Decomposing a Billing Service SPEC

SPEC describes a service that calculates ad-impression billing, exposes a REST API, and emits monthly invoices.

**Epics identified:**

1. `epic: Billing calculation engine` — pure logic, no I/O
2. `epic: Billing REST API` — exposes the engine
3. `epic: Monthly invoice generation` — scheduled job + storage

**Inside Epic 2 (REST API), the issue sequence:**

1. `feat(billing): Wire /billing/calculate and /billing/invoices endpoints with stubs` — skeleton
2. `feat(billing): Connect /billing/calculate to the calculation engine` — core
3. `feat(billing): Validate request payloads and return RFC 7807 errors` — edges
4. `feat(billing): Add structured logging and request metrics` — polish

Each issue body uses the 6-component frame from `references/templates.md`. Issue 1's Role is "Senior FastAPI engineer". Its Goal: "All four billing endpoints respond with valid response models and pass smoke tests." Its Constraints include the verbatim anti-bypass clause and a scope fence: "Do not implement real calculation logic — that is Issue 2's scope."

### Example 2: Single-Epic SPEC

User provides a short SPEC for a CLI tool that imports CSV files into PostgreSQL.

One epic is correct here — splitting would be over-engineering. The epic decomposes into four issues:

1. Skeleton — `cli import <file>` parses args, prints a stub plan, returns 0
2. Core — actually parse the CSV and stream rows to Postgres
3. Edges — handle bad rows, partial failures, resumable imports
4. Polish — progress bar, `--dry-run`, docs

Sequence is enforced by `Refs` keywords in each issue's Context: Issue 2 references Issue 1's PR, Issue 3 references Issue 2's, etc. This prevents agents from picking up Issue 3 before the skeleton lands.

### Example 3: When the SPEC Is Under-Specified

User pastes a one-paragraph SPEC: "Add notifications to the app."

Stop. Do not decompose. Reply with the gap analysis:

- What kinds of notifications? (email, push, in-app, all three?)
- Who triggers them? (user actions, system events, scheduled?)
- Delivery guarantees? (best-effort, at-least-once?)
- Out-of-scope items?

Ask via `AskUserQuestion` for the 2-3 most blocking gaps. A decomposition built on guesses creates work that needs to be redone.

## Troubleshooting

### Error: An issue body is missing 3+ of the 6 components

Rewrite it. The prompt-engineering skill exists precisely because vague issues produce vague work. If you cannot fill Context or Examples, the issue is premature — collapse it back into its parent epic until the scope is clear.

### Error: An epic has more than ~8 child issues

Split the epic. Tracer-code's "demoable at every step" guarantee breaks down at scale — too many in-flight issues means the system is no longer green between them. Two medium epics beat one giant epic.

### Error: You cannot define a skeleton issue for an epic

That epic is not vertically sliced. It is probably a horizontal refactor masquerading as a feature. Either:

- Re-scope it as a thin vertical slice that delivers one observable behavior end-to-end, or
- Demote it to a follow-up task that runs after the vertical slices land.

### Error: Issue numbers in cross-references are wrong after filing

You either filed children before epics (Step 8 ordering violation) or skipped the `sed` substitution. Edit the affected issues via `gh issue edit <N> --body-file <fixed-file>`; do not leave broken references in place.

### Error: User pushes back on the decomposition scope

This is the system working. Show the `git-issues/` files, explain the epic boundaries and the tracer-code sequencing, and iterate on disk before re-filing. The pre-filing review in Step 7 exists for exactly this.
