# INC-002: `creek review` is a stub

**Severity:** High
**Category:** INC
**Estimated complexity:** M (≤1d)
**Parallelizable with peers in same category:** partial — touches `creek/cli.py` like INC-001
**Discovered by:** Dimension 5 — confirmed by parallel agent

## Files affected
- `creek/cli.py:282-286` — `review()` body is `console.print("Would review:")`

## Dependencies
INC-001 (other CLI stubs). Should be tackled in the same pass.

## Blockers
The review queue is the documented exit valve for low-confidence classifications (`docs/classification.md:64-71`) and for `pending_review` redaction matches (`docs/redaction.md:78-79`). Without a working reviewer, ambiguous content sits in frontmatter forever or has to be edited by hand.

## Reproduction
```bash
$ creek review --vault ~/Obsidian/Creek-Vault
Would review: vault=/home/.../Creek-Vault
```

No TUI, no queue listing, no human decisions written.

## Analysis

Stub identical in shape to INC-001. The doc claims:

> `creek review` prints a TUI of pending fragments, lets you accept / override / defer each one, and writes the human decisions back to frontmatter as `method: manual`. Manual decisions are stable across re-classification — `creek classify` will not overwrite a `method: manual` field unless you pass `--force`.

The `ReviewQueueGenerator` (`creek/classify/review.py`) builds the queue. Nothing reads it back into a UI. The `--force` semantics (manual preservation across reclassify) is also unimplemented because INC-001's classify is itself a stub.

Confidence: verified.

## Proposed remediation

Implement an interactive Rich/Textual TUI (or simpler: a numbered prompt list) that reads `<vault>/00-Creek-Meta/Review-Queue.md` (or wherever the queue lives), displays each fragment with title, current classification, and confidence, and accepts: accept / override / defer / skip. On accept/override, write `classification.method = manual` into the fragment's frontmatter and remove the entry from the queue.

Alternative: skip the TUI; ship a non-interactive `creek review --list` that prints a table, plus `creek review --apply <fragment_id> --frequency F3 ...` that records a manual decision. Easier to test and more scriptable.

Whatever path, document it in `docs/classification.md` to match.

## Acceptance criteria

- `creek review --vault <vault>` lists pending fragments.
- A reviewer's decision is persisted as `classification.method: manual` in the fragment's frontmatter.
- A subsequent `creek classify --method rules` does not overwrite manual decisions unless `--force` is passed.
- An end-to-end test exercises the loop.

## References
- `creek-tools/docs/classification.md:64-71`
- `creek-tools/README.md:97`
- `creek/classify/review.py`
- INC-001
