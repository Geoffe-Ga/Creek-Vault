# FEAT-007: `creek state` wavelength snapshot, suggested questions, size-budget gate

**Severity:** High (v1.0)
**Category:** FEAT
**Estimated LOC:** ~450
**Estimated complexity:** M
**Source candidate:** [`plans/2026-05-05_comparative-analysis/candidates/ADOPT-005-audit-report-as-artifact.md`](../2026-05-05_comparative-analysis/candidates/ADOPT-005-audit-report-as-artifact.md) (part 2 of 2) + [`plans/2026-05-05_comparative-analysis/candidates/ADOPT-007-index-as-context-window-contract.md`](../2026-05-05_comparative-analysis/candidates/ADOPT-007-index-as-context-window-contract.md)
**Dependencies:** FEAT-006
**Parallelizable with peers:** no (extends FEAT-006's file)
**Wave:** 3

## Goal

Add the wavelength snapshot at the top of `creek state`'s output, generate phase-aware suggested questions, and pin a size budget on the rendered file so it stays context-window-sized.

## Files to touch

- `creek-tools/creek/generate/state.py` — add `section_wavelength_snapshot()`, `section_suggested_questions()`, and `section_liminal_watch()`.
- `creek-tools/creek/generate/wavelength.py` — extract a `current_phase_summary(vault)` helper if not already present.
- `creek-tools/creek/generate/mining.py` — expose a `phase_filtered_seeds(phase, n=5)` helper for the suggested-questions section.
- `creek-tools/tests/test_state.py` — add cases for the three new sections.
- `creek-tools/scripts/check-all.sh` — add a check that `00-Creek-Meta/State/latest.md` is under the documented size budget.

## Pre-decided choices

- **Wavelength snapshot at the very top** (before vault summary). Phase context interprets every other section — Karpathy's `index.md` discipline adapted to Creek.
- **Suggested questions:** 4–5 prompts pulled from `mining.phase_filtered_seeds(current_phase)`. Phase-aware: don't surface "draft your next Substack" during Bottoming Out; surface compost/synchronicity prompts instead.
- **Liminal Watch section:** recently surfaced unnamed clusters + growing tag clusters + fresh paradoxes. Inserted between Pre-LLM yield and Active eddies in FEAT-006's section order.
- **Size budget:** ≤50,000 tokens (≈200KB at typical token density). Enforced as a CI/`check-all.sh` line. Rationale: the audit report must fit in a single Claude context window so it can serve as the session-start prime for CrawDad and Claude Code.
- **Size-budget violations are quality signals, not bugs to suppress.** A failing budget means the compiled layer is fragmenting; the fix is to consolidate via `creek lint`, not to raise the cap.

## Test plan

- Unit: `section_wavelength_snapshot()` against a fixture vault returns the current phase, mode, dosage trends, and detected transitions in markdown.
- Unit: `section_suggested_questions()` filters by phase — Bottoming Out fixture surfaces compost/synchronicity prompts, Rising fixture surfaces draft/mine prompts.
- Unit: `section_liminal_watch()` surfaces fresh content from `10-Liminal/{Unnamed,Paradoxes}/` and growing tags.
- Regression: `latest.md` size-budget check fails when the file exceeds 50,000 tokens; the failure message names which sections grew.
- Integration: `creek state` end-to-end produces a file in the documented section order: wavelength → vault summary → pre-LLM yield → liminal watch → eddies → threads → synchronicities → hyperedges → drift warnings → suggested questions.

## Acceptance criteria

- Wavelength snapshot is the first section in the rendered audit report.
- Suggested questions are phase-aware (verified by fixture test).
- Liminal Watch section exists and pulls from `10-Liminal/`.
- A size-budget check fails the build when `latest.md` exceeds 50,000 tokens.
- `docs/generation.md` documents the budget and the rationale (fragmentation signal, not a cap to raise).
- ≥90% branch coverage on the changed paths.

## References

- Source candidates: ADOPT-005, ADOPT-007 (these were always one artifact).
- FEAT-006 (the file this extends).
- ADOPT-007 explicitly: the `index.md`-as-context-window contract.
- Wavelength infrastructure already in `creek/generate/wavelength.py`.
