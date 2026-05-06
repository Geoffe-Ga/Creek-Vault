# FEAT-006: `creek state` audit report — core sections

**Severity:** High (v1.0)
**Category:** FEAT
**Estimated LOC:** ~450
**Estimated complexity:** M
**Source candidate:** [`plans/2026-05-05_comparative-analysis/candidates/ADOPT-005-audit-report-as-artifact.md`](../2026-05-05_comparative-analysis/candidates/ADOPT-005-audit-report-as-artifact.md) (part 1 of 2)
**Dependencies:** FEAT-005 (consumes pre-LLM yield numbers)
**Parallelizable with peers:** yes (with FEAT-008, FEAT-009)
**Wave:** 3

## Goal

Add a `creek state` command that produces `00-Creek-Meta/State/<iso-week>.md` with the structural sections an agent or human needs to understand the vault: vault summary, eddies, threads, synchronicities, hyperedges, drift warnings, pre-LLM yield. Wavelength snapshot and suggested questions land in FEAT-007.

## Files to touch

- `creek-tools/creek/generate/state.py` (new) — `StateReportGenerator` with one method per section.
- `creek-tools/creek/cli.py` — add `@app.command() state(...)` near the existing `report` command (around line 771).
- `creek-tools/creek/generate/__init__.py` — export the new generator.
- `creek-tools/tests/test_state.py` (new) — section-by-section unit tests + integration test that verifies the rendered file structure.
- `creek-tools/docs/generation.md` — document `creek state`.

## Pre-decided choices

- **Output path:** `<vault>/00-Creek-Meta/State/<iso-year>-W<week>.md`. A symlink-or-copy at `<vault>/00-Creek-Meta/State/latest.md` always points at the most recent. Writing the symlink on Windows falls back to a copy.
- **Section order (this PR):** 1) Vault summary, 2) Pre-LLM yield, 3) Active eddies (top 10 by fragment count), 4) Active threads (top 10 by recency), 5) Surprising connections (synchronicities), 6) Hyperedges (praxis spanning multiple eddies), 7) Drift warnings (broken wiki-links + stale fragments). Wavelength + suggested questions are added by FEAT-007 between section 1 and 2.
- **`creek state` is a *view*, not a pipeline.** It re-reads existing vault state and emits the markdown; it does *not* re-run any expensive pass.
- **Empty sections render an explicit "no surfacing this week" note**, not a missing header.

## Test plan

- Unit: each `StateReportGenerator.section_*` method against a fixture vault returns deterministic markdown.
- Integration: `creek state` against a fixture vault writes the file at the expected path with all required sections in order.
- Regression: empty sections render the placeholder text rather than disappearing.
- Regression: re-running `creek state` the same week overwrites the file (idempotent).
- Regression: `latest.md` symlink/copy is updated after every run.

## Acceptance criteria

- `creek state` CLI exists, writes `00-Creek-Meta/State/<iso-week>.md` and `latest.md`.
- The seven sections listed above are present in order.
- A section with no content renders an explicit empty-state note.
- `creek state` does not re-run classification, linking, or compile — it only reads vault state.
- ≥90% branch coverage on `creek/generate/state.py`.
- `docs/generation.md` documents the command.

## References

- Source candidate: ADOPT-005 (full section list and Creek-flavored adaptations).
- FEAT-005 (pre-LLM yield JSONL is the input for section 2).
- FEAT-007 (wavelength snapshot + suggested questions sections + size-budget gate).
- Graphify's `GRAPH_REPORT.md` is the prior art (six structured sections).
