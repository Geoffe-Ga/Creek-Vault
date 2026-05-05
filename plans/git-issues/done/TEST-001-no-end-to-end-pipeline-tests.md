# TEST-001: Zero `@pytest.mark.e2e` tests; only six integration-marked tests

**Severity:** High
**Category:** TEST
**Estimated complexity:** L (>1d)
**Parallelizable with peers in same category:** yes — multiple e2e scenarios can be authored in parallel
**Discovered by:** Dimension 6; confirmed by parallel agent

## Files affected
- `creek-tools/tests/` (no e2e tests)
- `creek-tools/pyproject.toml:65-68` — markers defined but only `integration` used (6 tests)

## Dependencies
BUG-001, INC-001 — until those are fixed, an end-to-end test would mostly assert the broken behaviour.

## Blockers
This is the gap that masked BUG-001, BUG-008, and BUG-004. No test in the repo runs `creek process` against a real source dir and asserts that the resulting vault contains correct fragments with non-empty bodies, deterministic IDs, classification stubs, etc.

## Reproduction
```bash
grep -rn "@pytest.mark.e2e" creek-tools/tests/   # 0 hits
grep -rn "@pytest.mark.integration" creek-tools/tests/   # 6 hits
```

## Analysis

`creek-tools/CLAUDE.md` §6.1 claims "Test Types: Unit, Integration, and E2E coverage required." The pyproject.toml defines `integration` and `e2e` markers. There are zero e2e tests and only six integration tests, none of which test the full `Pipeline.run()` against a real source.

This is precisely why several of the Critical bugs in this review (BUG-001, BUG-004, BUG-008) escaped detection. Unit tests with mocked ingestors and writers don't catch "the pipeline drops your data." A test that puts a markdown file in `/tmp/source`, runs `Pipeline.run`, and asserts the resulting vault contains a non-empty markdown file with the right ID would have caught all three.

## Proposed remediation

Add `tests/e2e/` with:
- `test_full_pipeline_markdown.py` — drop a `.md` file in source, run Pipeline.run, assert the vault contains the expected fragment with deterministic ID and non-empty body.
- `test_full_pipeline_redaction.py` — drop a file with a fake secret, run pipeline, assert the secret isn't in the vault (catches BUG-004).
- `test_full_pipeline_idempotency.py` — run pipeline twice; assert second run writes zero new fragments.
- `test_full_pipeline_consent.py` — run pipeline against a new source without prior consent; assert ingestion is gated (catches INC-010).
- `test_purge_round_trip.py` — ingest, then purge by source, assert audit log is correct (catches INC-004 and SEC-005).
- `test_classify_review_round_trip.py` — ingest, classify, review, classify again; assert manual decisions persist (catches INC-002, INC-011).

Mark them all `@pytest.mark.e2e`. CI runs them in a separate job (see CI-003).

## Acceptance criteria

- At least 6 e2e tests exist.
- The pipeline-markdown test would have caught BUG-001 if it had been in place when the regression landed.
- E2E tests run in CI as a dedicated job.
- E2E tests use real disk I/O against tmp dirs, not mocks.

## References
- `creek-tools/CLAUDE.md` §6.1, §8
- `creek-tools/pyproject.toml:65-68`
