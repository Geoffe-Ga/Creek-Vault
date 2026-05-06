# FEAT-003: Compile primitive — `creek compile <fragment-id>`

**Severity:** High (v1.0)
**Category:** FEAT
**Estimated LOC:** ~450
**Estimated complexity:** M
**Source candidate:** [`plans/2026-05-05_comparative-analysis/candidates/ADOPT-001-three-layer-compiled-architecture.md`](../2026-05-05_comparative-analysis/candidates/ADOPT-001-three-layer-compiled-architecture.md) (part 1 of 2)
**Dependencies:** INC-019, FEAT-001 (compile.SKILL.md), FEAT-002 (paradox + liminal skills)
**Parallelizable with peers:** no (FEAT-004 depends on this)
**Wave:** 2 (compiled-layer primitives)

## Goal

Add a `creek compile` CLI command and supporting `creek/compile/` module that takes a fragment (or a directory of fragments) and produces / updates synthesis pages in `02-Threads/`, `03-Eddies/`, and `06-Frequencies/`. Per-source, manual trigger, interactive (discusses takeaways), with full provenance back to fragment IDs.

## Files to touch

- `creek-tools/creek/compile/__init__.py` (new) — public surface.
- `creek-tools/creek/compile/engine.py` (new) — the compile logic; takes a list of fragment IDs, an LLM client, and a target list of compiled-page paths to update.
- `creek-tools/creek/compile/provenance.py` (new) — `ProvenanceEntry` model + frontmatter writer (per-claim → fragment ID list).
- `creek-tools/creek/cli.py` — add `@app.command()` for `compile` near the existing classify/link block (around line 599–696).
- `creek-tools/creek/models.py` — add `CompiledPage` model with `provenance: list[ProvenanceEntry]` frontmatter.
- `creek-tools/tests/test_compile.py` (new) — unit + integration tests for the compile flow.

## Pre-decided choices

- **Compile granularity:** per-fragment (one `creek compile <fragment-id>` invocation = one source). Bulk mode (`creek compile --all-unsynthesized`) is deferred to v1.1.
- **Compiled page locations:** existing `02-Threads/`, `03-Eddies/`, `06-Frequencies/` directories. No new layer added; the *contract* is what changes.
- **Provenance schema:**
  ```yaml
  provenance:
    - claim_id: claim-001
      claim_excerpt: "first 80 chars of the claim"
      fragment_ids: [frag-9c1f3a2b8e02, frag-5d4e9c1a7f31]
      compiled_at: 2026-05-06T17:35:00Z
      compile_method: llm  # rules | llm | manual
  ```
- **Paradox handling during compile:** if the LLM detects contradictions across the fragments being compiled, it routes to `10-Liminal/Paradoxes/` (via FEAT-009's `creek save` once that exists; for this PR, log to `00-Creek-Meta/Processing-Log/paradoxes-during-compile.jsonl` and surface in the next lint run).
- **Privacy tier:** compile honours the source fragments' tiers. `intimate` fragments contribute title-only summaries to compiled pages; this matches existing `privacy_filter.py` behaviour.

## Test plan

- Unit: `engine.compile_fragments([frag1, frag2], target_eddy)` returns a `CompiledPage` with provenance for every claim.
- Unit: paradox detection during compile routes to the side-channel log, not into the synthesis page.
- Integration: `creek compile <fragment-id>` against a fixture vault produces an updated `02-Threads/` note with provenance frontmatter.
- Regression: `creek compile` against a vault with `intimate` fragments emits title-only contributions for those claims.
- Regression: re-running `creek compile <same-fragment-id>` is idempotent — same input produces a deterministic update (no duplicate claims, provenance lists merge).

## Acceptance criteria

- `creek compile <fragment-id>` exists and routes through `creek/compile/engine.py`.
- Compiled-page frontmatter carries the `provenance` list with one entry per claim, each linking back to fragment IDs.
- LLM-detected paradoxes during compile route to a side-channel log; they are *never* flattened into the synthesis page.
- `intimate`-tier fragments contribute title-only to compiled pages.
- Re-runs are idempotent (claim list merges, no dupes).
- ≥90% branch coverage on `creek/compile/`.

## References

- Source candidate: ADOPT-001 (full architecture rationale; especially the four-layer adaptation note).
- ADOPT-006 (confidence tiers — provenance entries should also carry the tier of the underlying edges; that lands in FEAT-006 follow-up).
- REJECT-004 (compiler-as-deterministic-binary is rejected; the verb is fine, the metaphor is not).
- Spec §10.2 (paradox preservation rules).
