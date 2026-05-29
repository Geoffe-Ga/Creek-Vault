# Backlog Grooming — 2026-05-29

## Scope

Reviewed the 20 most-recently-updated closed PRs and all 20 open issues in
`Geoffe-Ga/Creek-Vault`.

## Recently merged PRs and their issue resolution

All sub-issue PRs auto-closed their issues on merge (none of the referenced
sub-issues remain in the open list):

| PR | Closes | Notes |
|----|--------|-------|
| #359 | #350 | prompt-level ontology detection |
| #375 | #351 | AND→OR per-dimension retrieval |
| #381 | #352 | ontology-twist composition |
| #357 | #353 | `--signature-only` skills variant |
| #389 | #354 | `--seed-outline` multi-section composition |
| #380 | #355 | bidirectional grounding guard |
| #363 | #356 | unexplored-ontology mining |
| #378 | #365 | shared `WeightedFragmentClassification` model |
| #379 | #366 | LLM-driven weighted fragment classifier |
| #382 | #367 | holonic combine/decompose math module |
| #383 | #368 | bubble UP after splitting (reatomize) |
| #361 | (GAP-004) | scrub fragment-ID refs vault-wide |
| #374 | (GAP-005) | rewrite README key capabilities |
| #377 | (GAP-006) | scrub broken plans/git-issues cross-refs |
| #385 | (GAP-007) | align check-all.sh with CI gates |
| #386 | (GAP-008) | failure-mode coverage + typed embedding error |
| #396 | — | break clean.dedup ↔ ingest.base circular import |

## Actions taken

- **Closed epic #349** (`epic:voice` — voice-faithful synthesis) as completed.
  All seven sub-issues (#350–#356) merged to `main`. Marked the body checklist
  done with sub-issue→PR mapping and posted a completion comment.

## Reviewed, no action

- **Epic #364** (`epic:classify` — holonic weighted classifications): kept open.
  Sub-issues #365–#368 merged, but **#369** (aggregate-side bubble-up) is still
  open and its PR **#384 is open and under active review** (updated today, 21
  comments). Epic stays open until #384 merges.
- **#369**: kept open — awaiting #384.
- **#348** (bug: drafts truncate silently at `max_tokens`): kept open — no
  merged PR addresses it yet.
- Review follow-ups already filed and well-formed; no duplicates, no gaps to
  backfill: #370–#373 (purge/GAP-004 polish), #376 (docs), #387–#388
  (grounding-guard), #390–#393 (outline/draft polish + docstring issue-ref
  cleanup), #394–#395 (GAP-008 polish), #347 (FEAT-031 follow-up),
  #327 (ADAPT-003 Phase 3), #339 (lint unnamed-count bug).
- GAP-fix and chore PRs (#361, #374, #377, #385, #386, #396, #332, #362) are
  merged and their actionable follow-ups already exist. No retrospective
  issues needed.

## Issues created

None — the backlog had no uncovered gaps.

## Backlog health

- Open issues before: 20 (incl. completed epic #349).
- Open issues after: 19.
- Two live epics → one (#364) remains in progress via PR #384; #349 done.
- No duplicate or stale issues found; remaining open items are either active
  work (#369/#384) or well-scoped, labelled follow-ups.
