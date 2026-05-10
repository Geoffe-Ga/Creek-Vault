---
name: await-claude-review
description: >-
  Subscribe to GitHub PR activity and wait — without polling — for the
  Claude reviewer's verdict comment on the current HEAD. Use when a PR
  has just been pushed and you need to know the verdict
  (LGTM / CHANGES_REQUESTED / COMMENTS) before merging, addressing
  feedback, or declaring work done. Wraps
  `mcp__github__subscribe_pr_activity`, which delivers comments and CI
  failures (NOT CI passes) as `<github-webhook-activity>` events. The
  verdict comment itself is a comment event, so the bot's post wakes
  the session directly — no need to proxy through CI status. After
  subscribing, end the turn; events resume the session.
  Called by `address-feedback`, `stay-green`, and
  `comprehensive-pr-review`.
  Do NOT use for waiting on arbitrary CI success (not deliverable by
  the webhook), general PR babysitting unrelated to the verdict gate,
  debugging CI failures themselves (use `ci-debugging`), or one-off
  status polling (use `pull_request_read` directly).
metadata:
  author: Geoff
  version: 1.1.0
---

# Await Claude Review

Wait for the Claude reviewer's `Verdict:` comment on the current HEAD via PR webhook events. No polling, no `sleep`, no CI-pass proxy.

## What the Subscription Actually Delivers

`mcp__github__subscribe_pr_activity` says, verbatim:

> Once subscribed comments and CI failures will be delivered into this conversation as `<github-webhook-activity>` messages.

So the deliverable set is:

| Event                                         | Delivered? | Notes                                                          |
| --------------------------------------------- | ---------- | -------------------------------------------------------------- |
| Top-level PR comment (incl. reviewer verdict) | Yes        | This is the wake signal you want.                              |
| Line-level review comment / thread reply      | Yes        | Treated as a comment event.                                    |
| CI **failure** (any required check failing)   | Yes        | Triage on receipt — may indicate the reviewer action failed.   |
| CI **success / pass**                         | **No**     | Do not write logic that waits on this — it never arrives.      |
| Successful workflow_run completion            | **No**     | Same as above.                                                 |
| PR merge / close                              | Implicit   | You should `unsubscribe_pr_activity` on these from the caller. |

**Implication.** Don't treat "CI green" as a proxy for "review posted." The verdict comment is itself a delivered event — wait for it directly.

## Canonical Verdict Line

The reviewer (see `comprehensive-pr-review`) ends its top-level comment with a line matching, case-insensitive:

```
^\s*(?:##\s+|\*\*)?Verdict[:\*\s]+(LGTM|CHANGES_REQUESTED|COMMENTS)
```

Examples that match:

- `## Verdict: LGTM`
- `**Verdict:** CHANGES_REQUESTED`
- `Verdict: COMMENTS`

If the regex does not match, treat the comment as malformed: do **not** infer a verdict from prose ("looks good to me" is not a verdict). Surface to the user.

## Iteration-Trigger Wake (Owner-Authored Summary)

Some repos run an **iteration-trigger workflow** (`.github/workflows/iteration-trigger.yml`) that, after CI completes green, posts an executive-summary comment **as the repo owner** (via PAT) on the PR. The comment carries an HTML marker and a structured body:

```
<!-- iteration-trigger -->
**CI**: 5/5 Green
**VERDICT**: LGTM
**Action**: You are cleared to squash merge, delete the branch, ...
```

This comment is not authored by `claude[bot]` — it's authored by the human user via a PAT — but it **is** the wake signal you want when one is configured, because it bundles the verdict and the CI status into a single delivered event after the underlying claude-review comment has landed. Mobile-app sessions in particular rely on this trigger because their webhook recognises owner-authored comments.

**Recognition.** A comment qualifies as an iteration-trigger summary when **all** of:

1. The body contains the literal marker `<!-- iteration-trigger -->`.
2. A line matches `^\*\*VERDICT\*\*:\s*(LGTM|CHANGES[_ ]REQUESTED|COMMENTS)`.
3. A line matches `^\*\*CI\*\*:\s*(\d+)/(\d+)\s+Green` (use this to decide `merge` vs `iterate`).

**Routing.** When this comment wakes the session:

- `VERDICT == LGTM` and `CI: N/N Green` (full match) → caller proceeds to merge gate. The `Action:` line will say "cleared to squash merge…".
- `VERDICT == CHANGES_REQUESTED` or `VERDICT == COMMENTS` → caller enters fix loop. The `Action:` line will reference a comment ID to read for in-depth feedback. Pull that comment via `mcp__github__pull_request_read` `get_comments` and feed the body into `address-feedback`.
- `CI: x/y Green` where `x < y` → CI is not actually green; do not merge even on `LGTM`. Investigate the failing run before proceeding.

**Currency check still applies.** The trigger fires on a `workflow_run` for a specific HEAD SHA, but the comment may arrive minutes after the push. Use the standard `created_at >= headPushedAt` guard before treating it as authoritative for the current HEAD.

**Cap awareness.** The workflow caps itself at 10 self-posts per PR (it counts prior `<!-- iteration-trigger -->` markers). If you don't get a wake event after the eleventh push, the trigger has gone silent — fall back to checking the underlying claude-review comment directly via `get_comments`.

## Instructions

### Step 1: Pin the HEAD You're Waiting For

Before subscribing, record what "current" means so you can later distinguish a fresh verdict from a stale one.

1. `mcp__github__pull_request_read` with `method: "get"` → record `head.sha`.
2. `mcp__github__get_commit` with `sha: head.sha` → record `commit.committer.date` as `headPushedAt` (proxy for the latest push time).

### Step 2: Subscribe and End the Turn

```
mcp__github__subscribe_pr_activity
  owner: <owner>
  repo:  <repo>
  pullNumber: <N>
```

Then **stop**. Do not poll. Do not `sleep`. Do not call `pull_request_read get_comments` in a loop. Webhook events arrive as `<github-webhook-activity>` messages and resume the session on their own.

### Step 3: On Wake — Classify the Event

When a `<github-webhook-activity>` message arrives, decide what kind of event it is:

- **Owner-authored iteration-trigger summary** (body contains `<!-- iteration-trigger -->`) → go to Step 4a. This is the preferred wake on repos that run `iteration-trigger.yml`; it short-circuits the per-event classification because the verdict is already summarised.
- **Top-level PR comment from a reviewer bot** (`claude[bot]`, `github-actions[bot]`, or whichever account posts reviews on this repo) → go to Step 4.
- **Line-level review comment** → not a verdict; if you're tracking thread resolutions for `address-feedback`, handle there. Otherwise stay subscribed and wait for the next event.
- **CI failure event** → go to Step 5.
- **Anything else** → stay subscribed; wait for the next event.

### Step 4: Validate Currency and Parse Verdict

Re-fetch the comments to read the full body (the webhook payload may be truncated):

1. `mcp__github__pull_request_read` with `method: "get_comments"`.
2. Filter to bot author + body containing the verdict regex.
3. Sort by `created_at` desc; take the first.
4. **Currency check**: require `created_at >= headPushedAt`. A verdict posted before the latest push describes an earlier state; ignore and keep waiting.
5. Parse with the regex above. Return one of:
   - `LGTM` → caller proceeds to merge gate.
   - `CHANGES_REQUESTED` → caller enters fix loop.
   - `COMMENTS` → caller decides (usually mergeable as-is).
   - **Malformed** → surface to user; do not guess.

### Step 4a: Iteration-Trigger Summary

When the wake event is the owner-authored iteration-trigger comment:

1. **Currency check**: require `created_at >= headPushedAt`.
2. Parse `**VERDICT**:` and `**CI**: x/y Green` from the body.
3. If `VERDICT == LGTM` AND `x == y` (CI fully green): return `LGTM` to the caller. The follow-up `Action:` line typically reads "cleared to squash merge…". The caller proceeds to merge.
4. Otherwise: extract the comment ID referenced in `Action: pull comment <id> …`, fetch that comment's body via `get_comments`, and pass the body to the caller (typically `address-feedback`) for triage. Treat the underlying verdict (`CHANGES_REQUESTED` or `COMMENTS`) as authoritative for the next loop.
5. If the body is malformed (marker present but verdict line missing/unparseable): surface to user. Do not infer a verdict from the `Action:` prose.

### Step 5: On CI Failure for Current HEAD

A CI failure event for `head.sha` may mean the reviewer action itself failed (timeout, rate limit, permissions) — in which case **the verdict comment will never arrive** and waiting is futile. Inspect the failed check:

1. `mcp__github__pull_request_read` with `method: "get_check_runs"` for `head.sha`.
2. If the failed run is the **review action** (look for the workflow that runs `@claude` review): post `@claude please review` via `mcp__github__add_issue_comment` to retrigger, then stay subscribed.
3. If the failed run is **other CI** (lint, tests, build): surface the failure to the user and recommend handing off to `ci-debugging`. Stay subscribed — the reviewer action may still post.

### Step 6: Cleanup

The caller should call `mcp__github__unsubscribe_pr_activity` once the PR merges, closes, or the verdict gate is no longer needed. This helper does not unsubscribe on its own — leave the lifecycle to the caller.

## Examples

### Example 1: Push, Subscribe, Wake on LGTM

1. Caller (`address-feedback` Step 5) finishes pushing fixes.
2. Pin HEAD: `head.sha = abc123`, `headPushedAt = 2026-05-08T11:00:00Z`.
3. `subscribe_pr_activity owner=acme repo=widgets pullNumber=42`. End turn.
4. Wake: `<github-webhook-activity>` for a comment by `claude[bot]`.
5. `get_comments` → latest matching at `2026-05-08T11:04:33Z`, body ends `## Verdict: LGTM`. `11:04:33Z >= 11:00:00Z` ✓.
6. Return `LGTM` to caller. Caller proceeds to merge gate.

### Example 2: Stale Verdict After Force-Push

1. Pin HEAD: `head.sha = def456`, `headPushedAt = 2026-05-08T12:00:00Z`.
2. Subscribe, end turn.
3. Wake on a comment event. `get_comments` → latest verdict `LGTM` at `11:55:00Z` — that's *before* `headPushedAt`. The push superseded the verdict.
4. Stay subscribed. Optionally post `@claude please re-review`. Return to waiting.

### Example 3: Reviewer Action Failed in CI

1. Subscribe after push, end turn.
2. Wake: `<github-webhook-activity>` for a CI failure on `head.sha`.
3. `get_check_runs` → the failing job is `claude-review` (timeout). No other failures.
4. Post `@claude please review` to retrigger the action. Stay subscribed.
5. Subsequent wake delivers the verdict comment normally.

### Example 4: Other CI Failed; Verdict May Still Come

1. Subscribe after push, end turn.
2. Wake: CI failure on `head.sha`. `get_check_runs` → `pytest` failed; `claude-review` is still in progress.
3. Surface the test failure to the user and recommend `ci-debugging` for that. Stay subscribed — the reviewer may still post a verdict (which the author will need to address regardless of test failures).

## Troubleshooting

### Error: Tempted to wait on "CI green" / `workflow_run.conclusion == success`

Don't. The subscription does not deliver CI passes. Wait on the comment event directly — that *is* the delivered signal.

### Error: Tempted to poll with `sleep` or `Bash run_in_background`

Don't. The session is woken by `<github-webhook-activity>`. Polling burns time and conflicts with the harness's wake mechanism. Subscribe and end the turn.

### Error: Webhook arrives but `get_comments` shows no matching verdict

Possible causes, in order of likelihood:

1. The event was a line-level review comment, not the top-level verdict. Stay subscribed.
2. The reviewer posted a non-verdict comment (e.g., a status ping). Stay subscribed.
3. The bot author login differs on this repo. Confirm the author and update the filter.

### Error: Multiple bot accounts post comments

Match by author login (`claude[bot]`, `github-actions[bot]`) AND require the body to contain the canonical Verdict regex. If still ambiguous, ask the user which account is authoritative — do not guess.

### Error: PR merged or closed while waiting

The caller should detect this and call `unsubscribe_pr_activity`. If you see no further events for a long time, ask the user before assuming the PR is still open.
