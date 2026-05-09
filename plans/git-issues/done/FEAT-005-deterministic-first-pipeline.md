# FEAT-005: Deterministic-first pipeline vocabulary + `--no-llm` end-to-end

**Severity:** High (v1.0)
**Category:** FEAT
**Estimated LOC:** ~260
**Estimated complexity:** S
**Source candidate:** [`plans/2026-05-05_comparative-analysis/candidates/ADOPT-004-deterministic-first-pipeline.md`](../2026-05-05_comparative-analysis/candidates/ADOPT-004-deterministic-first-pipeline.md)
**Dependencies:** none (parallelizable with FEAT-001/002/003/004)
**Parallelizable with peers:** yes (independent module set)
**Wave:** 2

## Goal

Make Creek's already-deterministic-first pipeline *audit-able* and *strictly enforceable*. Document the Pass-1/Pass-2/Pass-3 split, add a `--no-llm` end-to-end flag, and emit a "pre-LLM yield" line per pipeline run.

## Files to touch

- `creek-tools/creek/pipeline.py` — add Pass-1/Pass-2/Pass-3 grouping, `--no-llm` plumbing, yield-reporting hook.
- `creek-tools/creek/cli.py` — add `--no-llm` flag to `process` (around line 379).
- `creek-tools/creek/audit/` — add a `pre_llm_yield_summary(run_id)` writer.
- `creek-tools/docs/configuration.md` and `docs/classification.md` — document the Pass-1/2/3 vocabulary and the privacy claim ("network egress only in Pass 3").
- `creek-tools/tests/integration/test_pipeline_no_llm.py` (new) — verifies a full `creek process --no-llm` run completes without any Anthropic-bound traffic.

## Pre-decided choices

- **Pass naming:** Pass 1 = local deterministic (ingest + redact + rules-classify + frontmatter); Pass 2 = local model-based (embeddings, OCR, future Whisper); Pass 3 = network if opted in (LLM classify, LLM compile, lint semantic checks).
- **`--no-llm` semantics:** runs Passes 1 and 2 to completion, skips Pass 3 entirely. Reports the residue (unclassified / low-confidence fragments).
- **Yield reporting format:** one structured line at end of run, written to stdout and to `00-Creek-Meta/Processing-Log/run-summary.jsonl`:
  ```
  Deterministic: N classified | Local-model: M embedded/OCR'd | Residue: K (would go to LLM if Pass-3 enabled)
  ```
- **CI test for no-network egress:** uses `pytest-socket` or equivalent to assert no outbound socket calls during a `--no-llm` run.

## Test plan

- Integration test: `creek process --no-llm` against a fixture source completes and produces a vault with rules-classified fragments. Network egress hooks see zero Anthropic traffic.
- Unit: the yield summary writer produces the documented JSONL line and the documented stdout format.
- Regression: passing `--no-llm` and `LLM_PROVIDER=anthropic` simultaneously still skips LLM (the flag wins).
- Documentation: a one-liner under `docs/configuration.md` shows the three-pass vocabulary.

## Acceptance criteria

- `docs/configuration.md` (or a sibling doc) names the three passes with the privacy claim verbatim: "network egress only in Pass 3."
- `creek process --no-llm` flag exists and works end-to-end.
- A run-summary JSONL line is emitted per pipeline run with deterministic / local-model / residue counts.
- A CI integration test verifies zero network egress during `--no-llm` runs.
- The pre-LLM yield is exposed in the audit report (FEAT-006 will surface it; this FEAT just produces it).
- No regression in existing pipeline behaviour when `--no-llm` is omitted.

## References

- Source candidate: ADOPT-004.
- Existing partial discipline: `docs/classification.md` already documents `--method rules` then `--method llm` on residue. This FEAT names the discipline explicitly.
- FEAT-006 (audit report) consumes the yield summary.
