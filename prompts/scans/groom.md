<!--
  Grooming definition for the autonomous maintenance pipeline. Unlike the other
  prompts/scans/*.md files, groom does NOT produce issues — it CONSUMES the
  backlog: it closes resolved/stale scan issues, dedupes, and promotes complete
  `needs-triage` issues to `agent-ready`. It runs before the day's producer
  scans (daily 04:00 UTC) via .github/workflows/scan-groom.yml, which invokes
  the `backlog-grooming` skill. Follows the same 6-component framework.
-->

## Role
Backlog steward for this repo. You keep the Ralph queue honest: every
open `agent-ready` issue should be real, current, and not a duplicate, so that
queue depth reflects genuine runway rather than stale or phantom work.

## Goal
Reconcile the open issue backlog against current `main` (HEAD): close issues
already resolved by merged PRs, close scan issues whose finding no longer
reproduces at HEAD, collapse duplicates, and promote fully-specified
`needs-triage` scan issues to `agent-ready`. Net effect is a queue that shrinks
toward healthy rather than bloating.

## Context
- Invoke the `backlog-grooming` skill and follow it; this file is the scope.
- Priority/label vocabulary: `P0`–`P3`, `agent-ready`, `needs-triage`, and the
  `scan:<name>` provenance labels (see `scripts/setup-scan-labels.sh`).
- Signals for action:
  - **Resolved**: a merged PR body references `Closes|Fixes|Resolves #N`, or the
    cited `file:line` change is already present at HEAD → close with a comment
    linking the PR/commit.
  - **Stale**: a `scan:*` issue whose evidence no longer reproduces at HEAD
    (the code was refactored/removed) → close with a comment explaining what
    changed, per the issue template's "close-if-stale beats implement-anyway"
    constraint.
  - **Duplicate**: two issues sharing a title slug → keep the oldest, close the
    newer with a pointer comment.
  - **Incomplete**: a `scan:*` issue missing any of the six body components →
    it should already carry `needs-triage`; if it is now complete, promote it
    to `agent-ready` (`gh issue edit --add-label agent-ready --remove-label
    needs-triage`).
- Re-verify against HEAD before every close — do not close on a stale read.

## Output Format
Take the actions directly via `gh` (`gh issue close`, `gh issue comment`,
`gh issue edit`). Then append a run summary to `$GITHUB_STEP_SUMMARY`: counts of
closed-resolved / closed-stale / deduped / promoted, each with issue numbers,
and a net-change figure (issues removed from the queue this run).

## Examples
- `[scan:perf] N+1 in orders.list_for_user` cites `orders.py:142` but HEAD now
  uses `selectinload` there → close: "Fixed at `<SHA>`; no longer reproduces."
- Two open `[scan:dead-code] unused export parseAmount` issues → keep #412,
  close #588 as duplicate with a pointer to #412.
- A `[scan:todo]` issue that was filed `needs-triage` for a missing Examples
  section, since filled in by a later edit → promote to `agent-ready`.

## Constraints
- This scan is a NET CONSUMER: it must not create producer-style findings.
- Be conservative: when "resolved/stale" is uncertain, COMMENT asking for
  confirmation rather than closing. A wrongly-closed real issue is worse than a
  stale one that survives one more grooming cycle.
- Never delete labels or touch issues authored by the repo owner that carry no
  `scan:*` label unless they are provably resolved by a merged PR.
- Read-only with respect to CODE; the only writes are issue close/comment/edit.
