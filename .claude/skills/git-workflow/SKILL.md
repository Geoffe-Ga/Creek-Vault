---
name: git-workflow
description: >-
  Issue-first development workflow with template-file-based gh commands and
  label hygiene. Use when creating issues, starting new work, making branches,
  committing changes, or opening PRs. Covers the full issue-to-PR lifecycle
  using file-based templates instead of inline strings. Do NOT use for
  bug-specific debugging or RCA (use bug-squashing-methodology), PR code review
  (use comprehensive-pr-review), or backlog cleanup and issue triage (use
  backlog-grooming).
metadata:
  author: Geoff
  version: 1.0.0
---

# Git Workflow

Issue-first development: every change starts with an issue, flows through a branch, and lands via a PR. Use template files for all gh commands instead of inline strings.

## Instructions

### Step 1: Check for Existing Issue

Before any work, search for an existing issue:

```bash
gh issue list --search "keyword" --state open
gh issue list --search "keyword" --state all
```

- If an issue exists, reference it and skip to Step 4.
- If no issue exists, confirm with the user before creating one.

### Step 2: Label Hygiene

Before creating an issue, check existing labels:

```bash
gh label list
```

- Use an existing label if one fits.
- Create a new label only if no similar one exists:
  ```bash
  gh label create "label-name" --description "What it covers" --color "hex"
  ```

### Step 3: Create Issue via Template File

Write the issue body to a file, then pass it to `gh`:

```bash
mkdir -p plans/github-issues
# Write body to plans/github-issues/ISSUE_NNN_slug.md
gh issue create \
  --title "type(scope): Brief description" \
  --body-file plans/github-issues/ISSUE_NNN_slug.md \
  --label "label1,label2"
```

Never inline long strings into `--body`. See `references/templates.md` for the issue body structure.

After creation, note the issue number for branch naming and commit references.

### Step 4: Branch from Issue

Create a branch referencing the issue number:

```bash
git checkout -b type/short-description-NNN
```

Branch type prefixes:
- `feat/` — new feature
- `fix/` — bug fix
- `chore/` — maintenance, deps, config
- `docs/` — documentation only

Example: `feat/add-user-search-42`, `fix/pagination-offset-87`

### Step 5: Commit via Message File

Write the commit message to a temp file, then commit with `-F`:

```bash
# Write message to plans/github-issues/COMMIT_NNN_slug.md
git commit -F plans/github-issues/COMMIT_NNN_slug.md
```

Use conventional commit format in the message. See `references/templates.md` for the commit message structure.

### Step 6: PR via Template File

Write the PR body to a file, then create the PR:

```bash
# Write body to plans/github-issues/PR_NNN_slug.md
gh pr create \
  --title "type(scope): Brief description" \
  --body-file plans/github-issues/PR_NNN_slug.md
```

The PR body must include `Closes #NNN` to auto-close the issue on merge. See `references/templates.md` for the PR body structure.

### Step 7: Post-Merge Cleanup

After the PR merges:

```bash
# Switch back to main and pull
git checkout main && git pull

# Delete the local branch
git branch -d type/short-description-NNN

# Verify the issue auto-closed
gh issue view NNN --json state
```

## Examples

### Example 1: Feature Development from Scratch

User asks: "Add a /health endpoint to the API."

**Search for existing issue:**
```bash
gh issue list --search "health endpoint" --state all
# No results
```

**Confirm with user**, then check labels and create issue:
```bash
gh label list
# "enhancement" exists, no need to create
```

Write `plans/github-issues/ISSUE_52_health-endpoint.md`:
```markdown
## Problem

The API has no health check endpoint. Load balancers and monitoring tools
need a lightweight endpoint to verify the service is running.

## Proposed Solution

Add a `GET /health` endpoint that returns `200 OK` with a JSON body
containing service status and version.

## Acceptance Criteria

- [ ] `GET /health` returns 200 with `{"status": "ok", "version": "x.y.z"}`
- [ ] Response time under 50ms (no DB calls)
- [ ] Endpoint is unauthenticated
```

```bash
gh issue create \
  --title "feat(api): Add /health endpoint" \
  --body-file plans/github-issues/ISSUE_52_health-endpoint.md \
  --label "enhancement"
# Created issue #52
```

**Branch and work:**
```bash
git checkout -b feat/add-health-endpoint-52
# ... implement the feature ...
```

**Commit via file** — write `plans/github-issues/COMMIT_52_health-endpoint.md`:
```
feat(api): add /health endpoint

Add lightweight health check endpoint for load balancer and monitoring
integration. Returns service status and version without hitting the
database.

Refs #52
```

```bash
git commit -F plans/github-issues/COMMIT_52_health-endpoint.md
```

**PR via file** — write `plans/github-issues/PR_52_health-endpoint.md`:
```markdown
## Summary

Add a `GET /health` endpoint that returns service status and version
for load balancer health checks.

## Changes

- Added `GET /health` route in `src/routes/health.ts`
- Returns `{"status": "ok", "version": "1.2.0"}`
- No authentication required, no database calls

## Test Plan

- [ ] `curl localhost:3000/health` returns 200
- [ ] Response body matches expected schema
- [ ] Response time under 50ms

Closes #52
```

```bash
gh pr create \
  --title "feat(api): Add /health endpoint" \
  --body-file plans/github-issues/PR_52_health-endpoint.md
```

### Example 2: Work Where Issue Already Exists

User asks: "Fix the broken pagination on the users list."

**Search for existing issue:**
```bash
gh issue list --search "pagination users" --state open
# Found: #87 "bug: Users list pagination returns duplicates"
```

Issue #87 already exists — skip issue creation, go straight to branching:

```bash
git checkout -b fix/pagination-offset-87
# ... fix the bug ...
```

**Commit via file** — write `plans/github-issues/COMMIT_87_pagination-fix.md`:
```
fix(users): correct pagination offset calculation

Off-by-one error in offset: page * limit should be (page - 1) * limit
since pages are 1-indexed.

Fixes #87
```

```bash
git commit -F plans/github-issues/COMMIT_87_pagination-fix.md
```

**PR via file** — write `plans/github-issues/PR_87_pagination-fix.md`:
```markdown
## Summary

Fix off-by-one pagination bug causing duplicate items across pages.

## Changes

- Fixed offset calculation in `src/services/users.ts:45`
- `offset = (page - 1) * limit` instead of `page * limit`

## Test Plan

- [ ] Page 1 and page 2 return no overlapping items
- [ ] Last page returns correct number of items
- [ ] Single-page result sets unaffected

Closes #87
```

```bash
gh pr create \
  --title "fix(users): Correct pagination offset" \
  --body-file plans/github-issues/PR_87_pagination-fix.md
```

## Troubleshooting

### Error: No matching labels found

If `gh label list` returns no relevant labels:
1. Check if the repo uses a different labeling convention (e.g., `type:bug` vs `bug`).
2. Create a new label only after confirming no similar one exists.
3. Use descriptive names and colors consistent with existing labels.

### Error: Issue already exists for this work

If `gh issue list --search` finds a matching issue:
1. Read the existing issue to confirm it covers the same scope.
2. If it does, reference it directly — do not create a duplicate.
3. If it's related but different, create a new issue and cross-reference.

### Error: PR description file path issues

If `gh pr create --body-file` fails:
1. Verify the file exists: `ls plans/github-issues/PR_NNN_slug.md`
2. Check for typos in the path — the directory may be `plan/` or `plans/`.
3. Ensure the file is not empty (gh rejects empty body files).
