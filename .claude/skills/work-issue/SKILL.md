---
name: work-issue
description: >-
  End-to-end GitHub issue implementation: read issue, branch, TDD, quality
  checks, self-review, and PR creation. Use when given an issue number to
  implement, asked to "work on issue #N", "pick up #N", "implement #N",
  or "start issue #N". Orchestrates stay-green, max-quality-no-shortcuts,
  and self-review skills. Do NOT use for bug triage (use bug-squashing-methodology),
  backlog maintenance (use backlog-grooming), or reviewing others' PRs
  (use comprehensive-pr-review).
metadata:
  author: Geoff
  version: 1.0.0
---

# Work Issue

Autonomous end-to-end implementation of a GitHub issue: read, branch, plan, build with TDD, pass all quality gates, self-review, and open a PR.

## Instructions

### Step 1: Read and Understand the Issue

Fetch the issue using the GitHub MCP tools:

```
mcp__github__issue_read(owner, repo, issue_number)
```

Extract and internalize:
- **Acceptance criteria** (the definition of done)
- **Requirements** (what to build)
- **Technical notes** (how to build it)
- **Dependencies** (what must exist first — verify these are satisfied)
- **File paths mentioned** (where to work)

Read every file referenced in the issue. Read related source files to understand the existing architecture. Do NOT start coding until you understand the surrounding code.

### Step 2: Create and Check Out the Branch

```bash
git fetch origin main
git checkout -b feat/ISSUE_NUMBER-short-description origin/main
```

Branch naming: `feat/` for features, `fix/` for bugs, `refactor/` for refactors. Include the issue number and a 2-4 word slug.

### Step 3: Plan the Implementation

Before writing any code, create a mental plan:
1. List the files you will create or modify
2. Identify which acceptance criteria map to which tests
3. Determine the order of implementation (what to build first)
4. Check for conflicts with parallel work (read recent PRs if unsure)

For complex issues (3+ files, new module), write a brief plan comment on the issue:
```
mcp__github__add_issue_comment(owner, repo, issue_number, body)
```

### Step 4: Build with TDD (Invoke stay-green)

Follow the stay-green skill strictly:

**For each piece of functionality:**

1. **Red** — Write a failing test that describes the behavior
   ```bash
   cd creek-tools && ./scripts/test.sh --all
   ```

2. **Green** — Write minimal code to make it pass
   ```bash
   cd creek-tools && ./scripts/test.sh --all
   ```

3. **Refactor** — Clean up while tests stay green

Work incrementally. One test at a time. Commit after each green cycle if the change is meaningful.

### Step 5: Enforce Quality (Invoke max-quality-no-shortcuts)

Throughout development, when any linter or type checker warns:
- STOP. Do NOT add `# noqa`, `# type: ignore`, or any bypass.
- Fix the root cause per the max-quality-no-shortcuts skill.
- The only acceptable bypasses are for third-party library bugs with full documentation.

### Step 6: Pass All Quality Gates

Run the full quality suite:
```bash
cd creek-tools && ./scripts/check-all.sh
```

This runs 7 checks: lint, format, typecheck, security, complexity, tests, coverage.

If anything fails:
1. Read the error carefully
2. Fix the root cause (not a bypass)
3. Re-run `./scripts/check-all.sh`
4. Repeat until exit 0

Then run pre-commit:
```bash
cd creek-tools && pre-commit run --all-files
```

Fix and re-run until all green. Work is NOT done until both commands exit 0.

### Step 7: Self-Review (Invoke self-review skill)

Before creating a PR, invoke the self-review skill:

```
/self-review
```

This runs a 6-section review of your own changes (bugs, architecture, security, ethics, testing, compliance). Address every finding rated BLOCKING or HIGH. Re-run quality gates after any fix.

### Step 8: Commit and Push

Stage only the files you changed:
```bash
git add creek-tools/creek/path/to/changed_files.py creek-tools/tests/test_files.py
git commit -m "feat(component): brief description (#ISSUE_NUMBER)"
git push -u origin feat/ISSUE_NUMBER-short-description
```

Use Conventional Commits. Reference the issue number in the commit message.

### Step 9: Create Pull Request

Create the PR using GitHub MCP tools:

```
mcp__github__create_pull_request(
  owner, repo,
  title="feat(component): brief description",
  body="## Summary\n- [bullets]\n\n## Test plan\n- [checklist]\n\nCloses #ISSUE_NUMBER",
  head="feat/ISSUE_NUMBER-short-description",
  base="main"
)
```

PR body must include:
- Summary (what and why)
- Test plan (how to verify)
- `Closes #N` to auto-close the issue on merge

### Step 10: Monitor CI and Review

Subscribe to PR activity:
```
subscribe_pr_activity(owner, repo, pr_number)
```

- If CI fails: invoke ci-debugging skill, fix, push.
- If Claude review requests changes: address each comment, push, re-request review.
- PR is merge-ready when: CI green AND Claude review LGTM.

## Examples

### Example 1: Implementing a New Module

```
User: /work-issue 28

Agent reads issue #28 (Embedding generation for fragments).
Agent checks out feat/28-embedding-generation.
Agent reads creek/link/embeddings.py (existing stub), creek/models.py, creek/config.py.
Agent plans: create EmbeddingLinker tests, implement generate_embeddings(), implement find_resonances().
Agent writes test_embedding_linker.py with 5 test cases (Red).
Agent implements generate_embeddings() (Green).
Agent runs check-all.sh (Green).
Agent invokes /self-review (LGTM).
Agent creates PR, subscribes to activity.
```

### Example 2: Enhancing an Existing Module

```
User: /work-issue 14

Agent reads issue #14 (Redaction scanner enhancements).
Agent checks out feat/14-redaction-scanner.
Agent reads creek/redact/scanner.py (existing code — 193 lines).
Agent reads issue requirements: multi-file-type, binary detection, tqdm, reports.
Agent plans incremental steps: binary detection first, then multi-type, then tqdm, then reports.
Agent TDDs each feature one at a time.
Agent runs check-all.sh after each feature.
Agent invokes /self-review when all features done.
Agent creates PR.
```

## Troubleshooting

### Error: Issue has unsatisfied dependencies
Check if dependency issues are closed. If a dependency is still open, tell the user:
"Issue #N depends on #M which is still open. Implement #M first or confirm the dependency is already satisfied in code."

### Error: Pre-commit hooks fail on commit
Do NOT use `--no-verify`. Read the error, fix the code, and commit again. If the hook is about commit message format, use Conventional Commits: `feat(scope): message (#N)`.

### Error: check-all.sh fails after self-review fixes
Re-run the full TDD cycle for any code you changed during self-review. Fixes can introduce new issues. Always verify with `./scripts/check-all.sh` as the final gate.

### Error: CI fails but local checks pass
Invoke the ci-debugging skill. Common causes: Python version differences, missing test markers, coverage config differences. Never assume "pre-existing issue" — your changes broke something.
