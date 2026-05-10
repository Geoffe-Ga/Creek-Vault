# Git Workflow Templates

## Issue Body Template

Write to `plans/github-issues/ISSUE_NNN_slug.md`:

```markdown
## Problem

[What is broken, missing, or needed. 2-3 sentences.]

## Context

[Why this matters. User impact, system impact, or business reason.]

## Proposed Solution

[How to fix or implement. Brief technical approach.]

## Acceptance Criteria

- [ ] Criterion 1
- [ ] Criterion 2
- [ ] Criterion 3

## Labels

[Suggested labels — verify against `gh label list` before applying.]
```

## PR Body Template

Write to `plans/github-issues/PR_NNN_slug.md`:

```markdown
## Summary

[1-3 sentences describing what this PR does and why.]

## Changes

- [Change 1 with file reference]
- [Change 2 with file reference]
- [Change 3 with file reference]

## Test Plan

- [ ] Test scenario 1
- [ ] Test scenario 2
- [ ] Test scenario 3

Closes #NNN
```

## Commit Message Template

Write to `plans/github-issues/COMMIT_NNN_slug.md`:

```
type(scope): brief description

[Body: 1-3 sentences explaining WHY, not just WHAT. Include context
that isn't obvious from the diff.]

[Footer: issue reference]
Refs #NNN
```

### Conventional Commit Types

| Type       | When to Use                        |
|------------|------------------------------------|
| `feat`     | New feature                        |
| `fix`      | Bug fix                            |
| `chore`    | Maintenance, deps, config          |
| `docs`     | Documentation only                 |
| `refactor` | Code change that doesn't add/fix   |
| `test`     | Adding or updating tests           |
| `ci`       | CI/CD pipeline changes             |

### Issue Reference Keywords

Use in commit messages and PR bodies:

| Keyword               | Effect on Merge              |
|-----------------------|------------------------------|
| `Closes #NNN`        | Auto-closes the issue        |
| `Fixes #NNN`         | Auto-closes the issue        |
| `Resolves #NNN`      | Auto-closes the issue        |
| `Refs #NNN`          | Links without closing        |

Use `Closes` in the PR body (auto-close on merge). Use `Refs` in commit messages (link without closing, since the PR handles closure).
