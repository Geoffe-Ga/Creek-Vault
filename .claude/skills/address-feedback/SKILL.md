---
name: address-feedback
description: >-
  Iterate on Claude PR review feedback intelligently and merge when ready.
  Use when the user asks to "address feedback", "respond to Claude's review",
  "iterate on the PR", "fix review comments", or "merge if Claude approved".
  Locates the most recent Claude LGTM via GitHub MCP, triages comments into a
  TDD-driven local fix loop, replies and resolves threads, and merges only
  when the latest LGTM matches HEAD and all required checks are green.
  Do NOT use for giving a review (use comprehensive-pr-review), debugging CI
  failures themselves (use ci-debugging), general TDD work outside review
  context (use stay-green), bug RCA (use bug-squashing-methodology), or
  issue/branch/PR creation (use git-workflow).
metadata:
  author: Geoff
  version: 1.0.0
---

# Address Feedback

Close the loop on a Claude PR review: find the latest LGTM (or feedback), iterate 
locally with TDD, push once, and merge only when the LGTM is current and CI is green.

## Prompt-Engineering Tactics (Brief)

Before touching code, restate each review thread as a 6-component micro-prompt so 
the fix is precise instead of sprawling:

- **Role** — "Engineer addressing a single review comment."
- **Goal** — the exact change requested (one sentence).
- **Context** — `file:line`, the surrounding 5-10 lines, the reviewer's quote.
- **Format** — minimal diff; no drive-by refactors.
- **Examples** — if the reviewer suggested code, paste it verbatim.
- **Constraints** — keep blast radius small; preserve public API; add a regression test.

If a comment is ambiguous on any component, reply asking for clarification rather 
than guessing. See `prompt-engineering` for the full framework.

## Instructions

### Step 1: Locate the Most Recent Claude LGTM (or Feedback)

Use the GitHub MCP tools — never `gh` CLI. The goal is to determine whether a 
current LGTM exists for `HEAD`, or what feedback is outstanding.

1. Get the PR's current head SHA:
   - `mcp__github__pull_request_read` with `method: "get"` → record `head.sha`.
2. List all reviews on the PR:
   - `mcp__github__pull_request_read` with `method: "get_reviews"`.
3. Filter to reviews authored by the Claude reviewer (login typically `claude` 
   or `claude[bot]`; if uncertain, also match `body` containing `LGTM` or 
   `CHANGES_REQUESTED`).
4. Sort by `submitted_at` descending. The first match is the most recent Claude 
   review.
5. Classify:
   - **Current LGTM** — `state == "APPROVED"` AND `commit_id == head.sha`. Skip 
     to Step 6.
   - **Stale LGTM** — `state == "APPROVED"` but `commit_id != head.sha`. Treat 
     as needing a fresh review after any further changes.
   - **Changes requested / commented** — gather all line comments via 
     `mcp__github__pull_request_read` with `method: "get_review_comments"`, 
     scoped to that review.
   - **No Claude review yet** — request one via `mcp__github__pull_request_review_write` 
     with `event: "REQUEST_CHANGES"` style request, then stop and tell the user.

### Step 2: Triage Comments into a Fix Plan

For each unresolved review comment, build a row:

| id | file:line | quote | requested change | test idea | severity |

Apply the prompt-engineering framing above. Drop or push back on comments that are 
out of scope, factually wrong, or already addressed — reply with a short justification 
instead of changing code.

### Step 3: Fix Locally with TDD — Never Push to Probe CI

For each row, smallest unit first:

1. **Red** — write a test that fails because of the bug the reviewer flagged.
2. **Green** — make the minimal change; the test passes.
3. **Refactor** — only within the same file, only if it stays green.

Then run the full local gate before any push:

```bash
# Whatever the project uses; pick the equivalents:
pre-commit run --all-files
./scripts/test.sh --all # or pytest / npm test / go test ./... / cargo test
./scripts/typecheck.sh # or mypy / tsc --noEmit / etc.
```

If a check fails, fix it locally and re-run. **Do not push to use CI as your test 
runner.** See `stay-green` for the gates and `ci-debugging` only if a local-green 
change later fails in CI.

### Step 4: Reply, Resolve, Re-Request

For each comment, after the fix lands locally:

1. `mcp__github__add_reply_to_pull_request_comment` — short reply: what changed, 
   where (`src/x.py:42`), and the commit SHA once pushed.
2. `mcp__github__resolve_review_thread` — only if the change is in and the reply 
   is posted.
3. After all threads are resolved, `mcp__github__pull_request_review_write` with 
   `event: "REQUEST_REVIEW"` (or post a fresh `@claude please re-review` comment 
   via `mcp__github__add_issue_comment`) so the next review is keyed to the new HEAD.

### Step 5: Push Once and Watch CI

Push the branch (single push, not one per comment). Optionally 
`mcp__github__subscribe_pr_activity` so review and CI events surface here. If CI 
fails, switch to `ci-debugging`; otherwise wait for the new Claude review.

### Step 6: Merge Gate — All Must Hold

Merge only when **every** condition is true. If any fails, stop and explain which one.

- Latest Claude review is `APPROVED`.
- That review's `commit_id == head.sha` (the LGTM is for the current HEAD, not 
  a stale one).
- All required status checks are `success` (`mcp__github__pull_request_read` with 
  `method: "get_status_checks"`).
- No unresolved review threads remain.
- The PR is `mergeable` and not `draft`.

Then:

```
mcp__github__merge_pull_request
pull_number: <N>
merge_method: "squash" # or whatever the repo standard is
```

Confirm the merge succeeded; do not delete the remote branch unless the user asks.

## Examples

### Example 1: Current LGTM, Green CI — Merge

1. `pull_request_read get` → `head.sha = abc123`.
2. `pull_request_read get_reviews` → latest Claude review: `APPROVED`, 
   `commit_id = abc123`.
3. `pull_request_read get_status_checks` → all `success`.
4. No unresolved threads. → `merge_pull_request` with `squash`. Report merge URL.

### Example 2: Stale LGTM After New Commits

1. Latest Claude review: `APPROVED`, `commit_id = def456`, but `head.sha = abc123`. 
   LGTM is stale.
2. State that the LGTM does not cover HEAD, request a fresh review via 
   `pull_request_review_write`, and **do not merge**.

### Example 3: CHANGES_REQUESTED with Three Comments

1. Triage table built from `get_review_comments`. Comment 2 is out of scope — 
   reply explaining why; skip the code change.
2. For comments 1 and 3: Red-Green-Refactor locally, run `pre-commit run --all-files` 
   and the test suite. All green.
3. Single `git push`. Reply on each thread with the change summary and SHA, then 
   `resolve_review_thread`.
4. `pull_request_review_write` requesting re-review. Wait. On next LGTM, re-enter 
   the merge gate (Step 6).

## Troubleshooting

### Error: Cannot tell which review is "Claude's"

Match by login (`claude`, `claude[bot]`) first. If that fails, match by 
`user.type == "Bot"` plus `body` containing `LGTM` / `CHANGES_REQUESTED`. If 
still ambiguous, ask the user which reviewer to treat as authoritative — do not guess.

### Error: LGTM exists but `commit_id` differs from HEAD

The LGTM is stale. Any commit, even a docs-only one, invalidates it for merge 
purposes. Request a fresh review and re-enter Step 6 only after the new approval 
lands on the current HEAD.

### Error: Reviewer's suggestion would break tests or public API

Do not silently ignore. Reply on the thread with the conflict (failing test name, 
API consumer, or constraint), propose an alternative, and pause until the user or 
reviewer agrees. Never bypass with `--no-verify` or skip checks; see 
`max-quality-no-shortcuts`.

### Error: Tempted to push to "see what CI says"

Stop. Reproduce the check locally first (`pre-commit run --all-files`, full test 
suite, typecheck). Pushing speculatively burns minutes per round trip and trains 
a sloppy loop. Only push when local gates are green.

### Error: Merge gate passes but `mergeable` is `false`

Conflicts with the base branch. Rebase or merge `main` locally, resolve, re-run 
local gates, push. The new commit invalidates the LGTM — request a fresh review 
before re-entering the merge gate.
