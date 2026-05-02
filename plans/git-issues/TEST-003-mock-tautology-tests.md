# TEST-003: Several tests assert on `mock.call_count` rather than behaviour

**Severity:** Medium
**Category:** TEST
**Estimated complexity:** M (≤1d)
**Parallelizable with peers in same category:** yes
**Discovered by:** Dimension 6 review

## Files affected
- `tests/test_embeddings.py:63`
- `tests/test_classify.py:661, 680, 1271, 1305`
- And other call sites flagged by `grep -rn "\.call_count\|assert mock" tests/`

## Dependencies
None.

## Blockers
None.

## Reproduction
```bash
grep -rn "\.call_count\|assert mock_" creek-tools/tests/ | wc -l
# >8 occurrences
```

For example `tests/test_classify.py:661` asserts `mock_client.messages.create.call_count == 3` without asserting that the resulting fragments actually carry the right classifications.

## Analysis

A test that confirms "the LLM was called 3 times" without verifying that the *output* matches expectations exercises the test infrastructure, not the production code. If the LLM client is later changed to call once with a 3-fragment batch, the test breaks even though behaviour is unchanged. If the call count happens to be 3 for the wrong reason, the test passes.

This is the canonical anti-pattern from the `comprehensive-pr-review` skill: "Tests that assert tautologies (e.g., `assert mock.called`)."

The system has 2678 tests passing, but if many are mock-call-count tests, the coverage and pass-count metrics overstate confidence. Combined with TEST-001 (no e2e tests), production-style failures aren't reliably caught.

## Proposed remediation

For each flagged test:
- Ask what user-visible behaviour the test is meant to assert.
- Replace mock-count assertions with state assertions on the returned fragments / writes / files.
- Where the count is genuinely the contract (e.g., "exactly one LLM call per batch"), keep it but pair with a behaviour assertion.

This is a one-time grooming task; new tests should follow the pattern.

## Acceptance criteria

- A grep for `\.call_count` in `tests/` returns near-zero results.
- Each remaining `mock.called`-style assertion has a doc comment explaining why call-count is the contract.
- The behaviour-style replacements continue to pass.

## References
- `tests/test_classify.py:661, 680, 1271, 1305`
- `tests/test_embeddings.py:63`
- `.claude/skills/comprehensive-pr-review/SKILL.md` (testing dimension)
