---
name: comprehensive-pr-review
description: >-
  Structured 10-section PR review covering security, quality, testing,
  and documentation. Use when reviewing pull requests, evaluating code
  changes, or doing code review. Produces verdicts with specific references.
  Do NOT use for backlog grooming or issue triage.
metadata:
  author: Geoff
  version: 1.0.0
---

# Comprehensive PR Review

Structured code review evaluating PRs across security, quality, testing, documentation, and project compliance.

## Instructions

### Step 1: Summarize the PR
Brief overview of what the PR does (2-3 sentences).

### Step 2: Identify Strengths
What is done well: good design decisions, well-written code, comprehensive tests, clear documentation, proper error handling.

### Step 3: Security Review
Flag security issues with severity:
- **BLOCKING**: Must fix before merge (injection, auth bypass, secrets exposure)
- **HIGH**: Should fix soon (missing input validation, weak crypto)
- **LOW**: Nice to have (hardening, defense-in-depth)

### Step 4: Identify Problems
Critical issues blocking merge: bugs, incorrect logic, failing tests, missing required features.

### Step 5: Evaluate Code Quality
Non-blocking improvements: readability, naming, organization, complexity.

### Step 6: Check Project Compliance
Verify against project standards (adjust to project's CLAUDE.md):
- Test coverage meets threshold
- Docstring coverage meets threshold
- Linting rules pass
- Type checking passes
- No forbidden patterns
- Conventional commits used

### Step 7: Assess Testing
- Test coverage adequate?
- Edge cases covered?
- Integration tests included?

### Step 8: Review Documentation
- Docstrings complete?
- README updated if needed?
- Examples provided for new APIs?

### Step 9: List Requests
Medium-priority suggestions that would improve the PR.

### Step 10: Deliver Verdict
- **LGTM** - Ready to merge
- **CHANGES_REQUESTED** - Must fix blocking issues
- **COMMENTS** - Suggestions only, can merge as-is

Include reasoning with specific file:line references.

## Examples

### Example 1: Approval with Suggestions

```markdown
## Summary
Adds user search endpoint with partial name/email matching.

## Strengths
- Clean separation of concerns (router -> service -> repository)
- Parameterized SQL queries prevent injection
- Good test coverage (94%)

## Security Concerns
None identified.

## Problems
None blocking.

## Code Quality
- Consider extracting the search query builder into its own method
- `search_users` could benefit from a `limit` parameter with default

## Verdict: LGTM
Well-structured PR with good test coverage. Suggestions are non-blocking.
```

### Example 2: Changes Requested

```markdown
## Summary
Adds file upload endpoint for user avatars.

## Security Concerns
- BLOCKING: No file type validation - allows arbitrary file upload (src/api/upload.py:23)
- HIGH: No file size limit - potential DoS vector (src/api/upload.py:15)

## Problems
- Missing error handling for disk full scenario (src/services/storage.py:45)
- Test doesn't verify file content, only status code (tests/test_upload.py:30)

## Verdict: CHANGES_REQUESTED
Security concerns must be addressed before merge. Add file type whitelist
and size limit validation.
```

## Troubleshooting

### Error: PR is too large to review effectively
- Ask the author to split into smaller PRs
- Focus on the most critical files first (API surface, security-sensitive code)
- Use `gh pr diff --name-only` to prioritize which files to review

### Error: Unclear what the PR is supposed to do
- Check for linked issues or a description
- Ask the author to add context before reviewing
- Review the test names - they often describe intended behavior
