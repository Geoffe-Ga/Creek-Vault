# FEAT-004: Query-path routing through compiled layer (`creek mine` / `creek draft`)

**Severity:** High (v1.0)
**Category:** FEAT
**Estimated LOC:** ~450
**Estimated complexity:** M
**Source candidate:** [`plans/2026-05-05_comparative-analysis/candidates/ADOPT-001-three-layer-compiled-architecture.md`](../2026-05-05_comparative-analysis/candidates/ADOPT-001-three-layer-compiled-architecture.md) (part 2 of 2)
**Dependencies:** FEAT-003 (compile primitive must exist)
**Parallelizable with peers:** yes (with FEAT-005, FEAT-006/007/008)
**Wave:** 2 (compiled-layer primitives)

## Goal

Make `creek mine` and `creek draft` route through the compiled layer first, falling back to fragments only when the compiled page is missing or insufficient. This is the operational commitment to compile-then-query; without it, the compiled layer exists but isn't the canonical query target.

## Files to touch

- `creek-tools/creek/generate/mining.py` — change the four discovery strategies to read from `02-Threads/`, `03-Eddies/`, `06-Frequencies/` first, fragments second. Today they read from fragments directly.
- `creek-tools/creek/generate/drafts.py` — change the source-material gathering step to pull from compiled pages (with provenance traversal back to fragments only when the compiled page is `inferred`-tier or missing).
- `creek-tools/creek/cli.py` — add `--bypass-compiled` flag to `mine` and `draft` as the documented escape hatch.
- `creek-tools/tests/test_mining.py` — add cases that verify compiled-layer-first behaviour.
- `creek-tools/tests/test_drafts.py` — same.
- `creek-tools/docs/generation.md` — document the routing change and the `--bypass-compiled` flag.

## Pre-decided choices

- **Routing precedence:** compiled-page exists → use it. Compiled-page missing → fall back to fragments and log a `compile-needed` entry to `00-Creek-Meta/Processing-Log/compile-gaps.jsonl` (lint surfaces these later).
- **Escape hatch flag:** `--bypass-compiled` (boolean, default `false`). Logs a warning when used so the operator knows they're side-stepping the discipline.
- **Provenance traversal during draft:** if a compiled-page claim has provenance fragment IDs, drafts can pull the original fragment text for exact-quote use; otherwise the draft works only from the compiled summary.

## Test plan

- Unit: `mining.run_strategy("thread-terminus", vault)` reads from `02-Threads/` notes, not fragment files.
- Unit: `drafts.gather_source_material(idea_seed)` pulls from compiled-page provenance fragment IDs when the compiled page exists.
- Regression: when no compiled page exists for a topic, mining still works against fragments AND a `compile-gaps.jsonl` entry is written.
- Regression: `creek mine --bypass-compiled` skips the compiled layer entirely and emits a warning to stderr.
- Regression test verifying the user-facing behaviour is unchanged when a compiled vault is present (the same essay seeds surface, perhaps in different ranking).

## Acceptance criteria

- `creek mine` and `creek draft` both check compiled pages before fragments.
- `--bypass-compiled` flag exists on both commands and is documented.
- Missing compiled pages log to `00-Creek-Meta/Processing-Log/compile-gaps.jsonl`.
- A regression test verifies that `creek draft` without `--bypass-compiled` routes through the compiled layer in the absence of `--bypass-compiled` (this is the AC pinned in ADOPT-001).
- ≥90% branch coverage on the changed paths.
- Documentation in `docs/generation.md` updated.

## References

- Source candidate: ADOPT-001 (especially "the contract is what changes" discussion).
- FEAT-003 (compile primitive that produces what this FEAT reads).
- ADOPT-006 / FEAT-006 (confidence tiers — drafts will eventually expose the tier mix; this FEAT just establishes routing).
