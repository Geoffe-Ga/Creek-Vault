# TEST-005: No property-based tests despite three obvious candidates

**Severity:** Low
**Category:** TEST
**Estimated complexity:** M (≤1d)
**Parallelizable with peers in same category:** yes
**Discovered by:** Dimension 6

## Files affected
- `creek-tools/tests/` (no `hypothesis` imports)

## Dependencies
None.

## Reproduction
```bash
grep -rn "from hypothesis\|import hypothesis\|@given" creek-tools/tests/
# (zero hits)
```

## Analysis

Three places where property-based testing would have caught real bugs surfaced in this review:

1. **Deterministic ID hashing (`creek/ingest/base.py:generate_fragment_id`).** Property: for any (source, ts, content), repeated calls return the same ID. Property: any two distinct triples return distinct IDs (high probability). Hypothesis can sample millions of triples.
2. **Frontmatter round-trip (`creek/vault/writer.py` ↔ `frontmatter.load`).** Property: `parse(write(model)) == model`. Hypothesis can generate `Fragment` instances and confirm round-trip equality.
3. **Redaction idempotency (`creek/redact/redactor.py`).** Property: applying redaction twice produces the same output as once. Hypothesis can fuzz inputs to catch off-by-one offsets in the patch logic.

These would not replace the unit tests; they'd add a layer that finds inputs the developer didn't think of.

## Proposed remediation

Add `hypothesis` to `requirements-dev.txt`. Add `tests/test_properties_*.py` for the three candidates above. Keep run-time bounded (`@settings(max_examples=200)` is plenty for CI).

## Acceptance criteria

- `tests/test_properties_id.py`, `test_properties_frontmatter.py`, `test_properties_redaction.py` exist.
- Each is `@given`-decorated and has at least 3 properties.
- Tests run in <30s in CI.
- A deliberate regression in `generate_fragment_id` (e.g., truncating to 8 chars instead of 12) is caught by the property test.

## References
- Hypothesis docs: <https://hypothesis.readthedocs.io/>
- `creek/ingest/base.py:generate_fragment_id`
- `creek/vault/writer.py`
- `creek/redact/redactor.py`
