---
name: address-feedback
description: >-
  Iterate on Claude PR review feedback intelligently and merge when ready.
  Use when the user asks to "address feedback", "respond to Claude's review",
  "iterate on the PR", "fix review comments", or "merge if Claude said LGTM".
  The Claude reviewer publishes a top-level PR comment via GitHub Action
  ending in a `Verdict:` line (LGTM / CHANGES_REQUESTED / COMMENTS) — it is
  NOT a formal GitHub approval. This skill locates the most recent such
  comment via GitHub MCP, parses the verdict, triages blockers/problems/nits
  into a TDD-driven local fix loop, replies and resolves threads, and merges
  only when the latest verdict is `LGTM`, the comment was posted after the
  current HEAD's push, and all required checks are green.
  Do NOT use for giving a review (use comprehensive-pr-review), debugging CI
  failures themselves (use ci-debugging), general TDD work outside review
  context (use stay-green), bug RCA (use bug-squashing-methodology), or
  issue/branch/PR creation (use git-workflow).
metadata:
  author: Geoff
  version: 1.1.0
---

# Address Feedback

Close the loop on a Claude PR review: find the latest verdict comment, iterate locally with TDD, push once, and merge only when the verdict is `LGTM` for the current HEAD and CI is green.

## How the Claude Review Surfaces

The Claude reviewer runs as a GitHub Action on each push. It posts its findings as a **top-level PR comment** authored by a bot account (e.g. `claude[bot]`, `github-actions[bot]`). The comment follows the `comprehensive-pr-review` format and ends with a line like:

```
## Verdict: LGTM
```

Possible verdicts: `LGTM`, `CHANGES_REQUESTED`, `COMMENTS`. There is **no formal GitHub approval** to read — `state == "APPROVED"` will not be set. Treat the comment body as the source of truth.

## Prompt-Engineering Tactics (Brief)

Before touching code, restate each review item as a 6-component micro-prompt so the fix is precise instead of sprawling:

- **Role** — "Engineer addressing a single review comment."
- **Goal** — the exact change requested (one sentence).
- **Context** — `file:line`, the surrounding 5-10 lines, the reviewer's quote.
- **Format** — minimal diff; no drive-by refactors.
- **Examples** — if the reviewer suggested code, paste it verbatim.
- **Constraints** — keep blast radius small; preserve public API; add a regression test.

If a comment is ambiguous on any component, reply asking for clarification rather than guessing. See `prompt-engineering` for the full framework.

## Instructions

### Step 1: Locate the Latest Claude Verdict Comment

Use the GitHub MCP tools — never `gh` CLI. The goal is to determine whether a Claude review comment exists for the current HEAD push, and what its verdict is.

1. Get HEAD SHA and the push timestamp:
   - `mcp__github__pull_request_read` with `method: "get"` → record `head.sha`.
   - `mcp__github__get_commit` with `sha: head.sha` → record `commit.committer.date` (proxy for the latest push time).
2. List **top-level PR comments** (not line-level review comments):
   - `mcp__github__pull_request_read` with `method: "get_comments"` (paginate if the PR is long-running).
3. Filter the comments:
   - **Author** is a bot matching the Claude reviewer (`claude[bot]`, `github-actions[bot]`, or whichever account posts the review on this repo). When in doubt, also require the body to contain a `Verdict:` line.
   - **`created_at >= head commit's committer.date`** — the currency check. Comments posted before the latest push describe an earlier state and are stale.
4. Sort matching comments by `created_at` desc; the first is the **current** Claude review.
5. Parse the verdict from that comment's body. Look for a line matching (case-insensitive):

   ```
   ^\s*(?:##\s+|\*\*)?Verdict[:\*\s]+(LGTM|CHANGES_REQUESTED|COMMENTS)
   ```

6. Classify and route:
   - `LGTM` → skip to Step 6 (merge gate).
   - `CHANGES_REQUESTED` → required fixes; continue to Step 2 with the **Security Concerns**, **Problems**, and any blocking items from the comment body.
   - `COMMENTS` → optional improvements; user decides whether to address; if skipping, jump to Step 6.
   - **No qualifying comment** (none after the latest push) → wait for the next review run; do not merge. Optionally post `@claude please review` via `mcp__github__add_issue_comment` if the action did not run.
   - **Comment exists but no parseable Verdict line** → treat as malformed; ask the user before merging. Do not infer a verdict from prose.

### Step 2: Triage the Comment Body into a Fix Plan

The Claude review is a single comment with sections (Strengths / Security Concerns / Problems / Code Quality / Requests / Verdict). Extract each actionable item into a row:

| id | section | file:line (if cited) | quote | requested change | test idea | severity |

Also pull any **line-level** review threads via `mcp__github__pull_request_read` with `method: "get_review_comments"` and merge them into the same table — these come back with `isResolved` metadata so you can ignore already-resolved threads.

Apply the prompt-engineering framing above. Drop or push back on items that are out of scope, factually wrong, or already addressed — reply with a short justification instead of changing code.

### Step 3: Fix Locally with TDD — Never Push to Probe CI

For each row, smallest unit first:

1. **Red** — write a test that fails because of the bug the reviewer flagged.
2. **Green** — make the minimal change; the test passes.
3. **Refactor** — only within the same file, only if it stays green.

Then run the full local gate before any push:

```bash
# Whatever the project uses; pick the equivalents:
pre-commit run --all-files
./scripts/test.sh --all      # or pytest / npm test / go test ./... / cargo test
./scripts/typecheck.sh       # or mypy / tsc --noEmit / etc.
```

If a check fails, fix it locally and re-run. **Do not push to use CI as your test runner.** See `stay-green` for the gates and `ci-debugging` only if a local-green change later fails in CI.

### Step 4: Reply, Resolve, Re-Request

For each item, after the fix lands locally:

1. **Line-level threads** — `mcp__github__add_reply_to_pull_request_comment` with a short reply (what changed, where: `src/x.py:42`, and the commit SHA once pushed), then `mcp__github__resolve_review_thread`.
2. **Top-level Claude review comment** — there is no thread to resolve. Post a single summary reply via `mcp__github__add_issue_comment` listing each addressed item and the SHA(s) that fixed it.
3. After pushing, request a fresh review by posting `@claude please re-review` via `mcp__github__add_issue_comment`. The GitHub Action runs again and writes a new verdict comment — that becomes the comment you parse on the next pass.

### Step 5: Push Once and Watch CI

Push the branch (single push, not one per fix). Optionally `mcp__github__subscribe_pr_activity` so review and CI events surface here. If CI fails, switch to `ci-debugging`; otherwise wait for the new Claude verdict comment.

### Step 6: Merge Gate — All Must Hold

Merge only when **every** condition is true. If any fails, stop and explain which one.

- Latest qualifying Claude review comment has `Verdict: LGTM`.
- That comment's `created_at >= head commit's committer.date` (verdict is for the current HEAD, not a pre-push state).
- All required check runs are `success`:
  - `mcp__github__pull_request_read` with `method: "get_status"` (combined commit status), and
  - `mcp__github__pull_request_read` with `method: "get_check_runs"` (per-job detail).
- No unresolved line-level review threads (`mcp__github__pull_request_read` with `method: "get_review_comments"` — each thread has `isResolved`).
- The PR is `mergeable` and not `draft` (from the `get` response).

Then:

```
mcp__github__merge_pull_request
  pull_number: <N>
  merge_method: "squash"   # or whatever the repo standard is
```

Confirm the merge succeeded; do not delete the remote branch unless the user asks.

## Examples

### Example 1: Current `Verdict: LGTM`, Green CI — Merge

1. `pull_request_read get` → `head.sha = abc123`. `get_commit abc123` → `committer.date = 2026-05-01T10:00:00Z`.
2. `pull_request_read get_comments` → latest bot comment by `claude[bot]` at `2026-05-01T10:04:33Z`, body ends with `## Verdict: LGTM`.
3. `10:04:33Z >= 10:00:00Z` → comment is current.
4. `get_status` and `get_check_runs` → all `success`. `get_review_comments` → no unresolved threads. PR `mergeable: true`, `draft: false`.
5. `merge_pull_request` with `squash`. Report merge URL.

### Example 2: Verdict Comment Predates the Latest Push (Stale)

1. `head.sha = abc123`, `committer.date = 11:30:00Z`.
2. Latest Claude comment is `Verdict: LGTM` but `created_at = 09:15:00Z` — before the push that produced `abc123`.
3. The verdict reflects an earlier HEAD. State that the LGTM is stale, post `@claude please re-review` via `add_issue_comment`, and **do not merge**.

### Example 3: `Verdict: CHANGES_REQUESTED` with Two Blockers and a Nit

1. Parse the comment body: two **Problems** (file:line cited) and one **Code Quality** nit. Build the triage table.
2. Decide the nit is out of scope for this PR — reply on the top-level Claude comment justifying the deferral.
3. For the two blockers: Red-Green-Refactor locally, then `pre-commit run --all-files` + full test suite + typecheck. All green.
4. Single `git push`. Post a summary reply via `add_issue_comment` listing the addressed items and the SHA. Then post `@claude please re-review`.
5. New Claude comment arrives with `Verdict: LGTM` after the new push timestamp → re-enter Step 6.

## Troubleshooting

### Error: Cannot tell which comment is "Claude's"

Match by author login first (`claude[bot]`, `github-actions[bot]`); fall back to `user.type == "Bot"` plus a body that contains a `Verdict:` line. If still ambiguous, ask the user which bot to treat as authoritative — do not guess.

### Error: Verdict line not found or malformed

The reviewer is supposed to end with `## Verdict: LGTM | CHANGES_REQUESTED | COMMENTS`. If the regex does not match, do not infer the verdict from prose ("looks good to me" is not a verdict). Surface the malformed comment to the user, optionally re-request the review, and **do not merge**.

### Error: Verdict comment exists but predates the HEAD push

The LGTM was for an earlier commit. Any push, even a docs-only one, supersedes it. Re-request a review (`add_issue_comment` with `@claude please re-review`), wait for the new comment, and re-enter Step 6 only after a current `Verdict: LGTM` arrives.

### Error: Reviewer's suggestion would break tests or public API

Do not silently ignore. Reply on the relevant thread (or the top-level comment) with the conflict (failing test name, API consumer, or constraint), propose an alternative, and pause until the user or reviewer agrees. Never bypass with `--no-verify` or skip checks; see `max-quality-no-shortcuts`.

### Error: Tempted to push to "see what CI says"

Stop. Reproduce the check locally first (`pre-commit run --all-files`, full test suite, typecheck). Pushing speculatively burns minutes per round trip and trains a sloppy loop. Only push when local gates are green.

### Error: Merge gate passes but `mergeable` is `false`

Conflicts with the base branch. Rebase or merge `main` locally, resolve, re-run local gates, push. The new commit supersedes the LGTM verdict — request a fresh review before re-entering the merge gate.
