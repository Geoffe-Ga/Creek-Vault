---
name: backlog-grooming
description: >-
  Systematic GitHub backlog maintenance: review merged PRs, close resolved
  issues, identify gaps, and create missing issues. Use when asked to
  groom the backlog, clean up the issue tracker, or review recent work.
  Do NOT use for PR code review (use comprehensive-pr-review).
metadata:
  author: Geoff
  version: 1.0.0
---

# Backlog Grooming

Systematically review recent work, close resolved issues, identify gaps, and maintain a clean issue backlog.

## Instructions

### Step 1: Initialize Progress Tracking

Create a dated progress file:
```bash
DATE=$(date +%Y-%m-%d)
PROGRESS_FILE="plan/${DATE}_BACKLOG_GROOMING.md"
```

### Step 2: Fetch and Analyze Recent PRs

```bash
gh pr list --state merged --limit 15 --json number,title,mergedAt,body,url
```

For each PR, extract:
- Issues referenced (closes #N, fixes #N, resolves #N)
- Work done (features, fixes, refactors)
- Gaps (work done without corresponding issues)

### Step 3: Verify Issue Resolution

For each issue referenced in PRs:
```bash
gh issue view 123 --json state,title,labels
```

Decision matrix:
- **Fully resolved** -> Close with comment referencing PR
- **Partially resolved** -> Update with progress comment
- **Not resolved** -> Keep open, remove incorrect PR reference
- **Spawned new work** -> Create follow-up issue, close original

### Step 4: Close Resolved Issues

```bash
gh issue close 123 --comment "Resolved in PR #456. [Description of fix]. Changes: [list]. Closes via: [PR URL]"
```

### Step 5: Check for Duplicates Before Creating New Issues

```bash
gh issue list --search "keyword" --state all
gh issue list --label "bug" --state open
```

### Step 6: Create Missing Issues

For gaps identified (features, bugs fixed, follow-up work) that have no existing issue:
```bash
gh issue create --title "Issue title" --body "Description" --label "label1,label2"
```

### Step 7: Finalize Progress File

Complete the summary with statistics: PRs analyzed, issues closed, issues created, issues updated, backlog health before/after.

## Examples

See `references/templates.md` for issue close/create templates and `references/example-session.md` for a complete grooming session walkthrough.

### Example 1: Closing a Resolved Issue

```bash
gh issue close 123 --comment "Resolved in PR #456

Added input validation for single-object payloads.

Changes:
- Added type check in process_input()
- Added test for single-object case

Thank you for reporting! Fix is merged and available."
```

### Example 2: Creating a Follow-Up Issue

```bash
gh issue create --title "perf: Optimize database queries in user search" \
  --body "During PR #458, identified N+1 query in user search. Need to add eager loading." \
  --label "enhancement,performance"
```

## Troubleshooting

### Error: Too many PRs to review
- Focus on most recent PRs first
- Split into multiple sessions, document stopping point
- Default to last 15 PRs unless specified otherwise

### Error: Can't find issues referenced in PR
- Check PR body AND individual commit messages
- Look for variations: "close", "fix", "resolve" (with/without #)
- If genuinely missing, create retrospective issue

### Error: Found many duplicate issues
- Consolidate into a canonical issue
- Close duplicates with link to canonical
- Consider improving issue labels/search to prevent future duplicates
