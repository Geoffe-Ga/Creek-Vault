# FEAT-008: `creek lint` — unified vault hygiene operation

**Severity:** High (v1.0)
**Category:** FEAT
**Estimated LOC:** ~530
**Estimated complexity:** M
**Source candidate:** [`plans/2026-05-05_comparative-analysis/candidates/ADOPT-002-creek-lint-unified-hygiene.md`](../2026-05-05_comparative-analysis/candidates/ADOPT-002-creek-lint-unified-hygiene.md)
**Dependencies:** FEAT-001 (lint.SKILL.md guard rails), FEAT-002 (paradox skill), FEAT-006 (audit-report append target)
**Parallelizable with peers:** yes (with FEAT-007 and FEAT-009; FEAT-006 must land first)
**Wave:** 3

## Goal

Unify the five existing emergence reports (Tag Garden, Unnamed Digest, Synchronicity, Compost, Paradox) under one named `creek lint` operation with per-check selectors. Add deterministic checks (broken wiki-links, schema-skill size budgets, orphan compiled pages) and an incremental `--since` mode.

## Files to touch

- `creek-tools/creek/lint/__init__.py` (new) — public surface.
- `creek-tools/creek/lint/runner.py` (new) — orchestrator that dispatches to individual check modules.
- `creek-tools/creek/lint/checks/` (new directory) — one file per check: `paradox.py`, `unnamed.py`, `synchronicity.py`, `compost.py`, `tags.py` (these wrap the existing `creek/generate/*` modules), plus deterministic checks: `broken_links.py`, `orphan_compiled.py`, `skill_size_budget.py`.
- `creek-tools/creek/cli.py` — add `@app.command() lint(...)` with `--check NAME` and `--since DURATION` flags.
- `creek-tools/creek/generate/{paradox,unnamed,synchronicity,compost,tags}.py` — make their entry points reusable by `creek/lint/checks/*`. Existing `creek report --type X` commands stay as thin wrappers that call into the new lint check modules.
- `creek-tools/tests/test_lint.py` (new) — orchestrator tests + per-check tests.

## Pre-decided choices

- **Default `creek lint` runs all deterministic checks always; semantic checks (paradox, synchronicity, unnamed) require `--full` or `--since`.** Rationale: deterministic checks are cheap (link-graph traversal); semantic checks use embeddings and may take minutes.
- **Output:** consolidated markdown report appended to the next `creek state` audit-report run, plus a per-run summary written to `00-Creek-Meta/Processing-Log/lint-<iso-date>.md`.
- **Lint never resolves paradoxes.** The `creek/lint/checks/paradox.py` wrapper preserves the existing `ParadoxDetector` behaviour: contradictions route to `10-Liminal/Paradoxes/`, never to a "to-fix" queue. Pinned by a regression test.
- **Lint never auto-creates compiled pages.** Missing-compiled-page checks emit suggestions only.
- **Lint never deletes orphan fragments.** Orphan fragments are normal; only orphan *compiled* pages are flagged.
- **`--check NAME` accepts:** `paradox`, `unnamed`, `synchronicity`, `compost`, `tags`, `broken-links`, `orphan-compiled`, `skill-size`. Multiple `--check` flags allowed.
- **`--since DURATION`:** `7d`, `1w`, `1mo`, `30d`. Deterministic checks always run incrementally given `--since`; semantic checks only run incrementally if their underlying detector supports it (today, none do — falls back to full run).

## Test plan

- Unit per check module.
- Integration: `creek lint` with no args runs all deterministic checks against a fixture vault and produces the expected lint report.
- Integration: `creek lint --check paradox` runs only the paradox check.
- Integration: `creek lint --since 7d` runs deterministic checks incrementally.
- Regression: a paradox detected during lint lands in `10-Liminal/Paradoxes/`, not in a "to-fix" list. (This is the AC pinned in ADOPT-002.)
- Regression: lint never creates a compiled page — even when the missing-compiled-page check fires, it emits a suggestion only.
- Regression: existing `creek report --type {tags,unnamed,synchronicity,compost,paradox}` commands still work (they're now thin wrappers).

## Acceptance criteria

- `creek lint` CLI exists with `--check` and `--since` flags as documented.
- All five existing emergence report types are reachable as `--check` values.
- Three new deterministic checks exist: `broken-links`, `orphan-compiled`, `skill-size`.
- The "no resolve, no create, no delete" rules are documented in the lint module's docstring AND verified by regression tests.
- Lint output appends to the next `creek state` run (FEAT-006/007).
- ≥90% branch coverage on `creek/lint/`.
- `docs/emergence.md` and a new `docs/lint.md` document the command.

## References

- Source candidate: ADOPT-002 (especially the "do not resolve paradoxes" guard rail).
- Existing modules being unified: `creek/generate/{paradox,unnamed,synchronicity,compost,tags}.py`.
- `cablate/llm-atomic-wiki` (community Karpathy implementation) for the deterministic-vs-semantic split inspiration.
