# TEST-002: Aggregate-only coverage gate hides under-tested modules (presentations 67%, documents 78%, gdrive 80%)

**Severity:** Medium
**Category:** TEST
**Estimated complexity:** S (≤2h)
**Parallelizable with peers in same category:** yes
**Discovered by:** Dimension 6

## Files affected
- `creek-tools/pyproject.toml:89` — `fail_under = 90` (single number, aggregate)
- `creek-tools/.github/workflows/ci.yml` — same threshold applied at the aggregate level
- Low-coverage modules: `creek/ingest/presentations.py` (67.61%), `creek/ingest/documents.py` (77.99%), `creek/ingest/gdrive.py` (80.79%)

## Dependencies
None.

## Blockers
None.

## Reproduction
```bash
python3 -m pytest --cov=creek --cov-branch tests/ | grep -E "presentations|documents|gdrive"
# creek/ingest/presentations.py     114     30     28      4  67.61%
# creek/ingest/documents.py         198     41     70      8  77.99%
# creek/ingest/gdrive.py            248     40     54      6  80.79%
```

The aggregate sits at 93.6% so the gate passes, despite three modules well below the project's documented 90% threshold.

## Analysis

`pyproject.toml`:
```toml
[tool.coverage.report]
fail_under = 90
```

This is an aggregate gate — total covered statements / total statements. Modules can fall well below 90% individually as long as they're outweighed by well-covered modules.

For ingestors specifically, the under-tested branches tend to be error paths: optional-dep ImportError handling, malformed-input fallbacks, and rare format edge cases. Those are exactly the paths that fail in production. Sub-90% coverage on `presentations.py` (the entire `python-pptx`-unavailable fallback is uncovered) and `gdrive.py` (network error handling) is meaningful risk.

## Proposed remediation

Add a per-file minimum. Options:
- `coverage report --include="creek/ingest/*.py" --fail-under=85` as a second gate.
- A small Python helper that reads `coverage.json` and asserts every file's `summary.percent_covered >= 85` (or 90, whatever the project decides). Run in CI.
- Use `pytest-cov`'s `--cov-fail-under` only for the aggregate and add a separate `coverage report --fail-under=85 --skip-covered` style check that fails per-file below the threshold.

Decide on a per-file threshold (suggest 80%) that's lower than the aggregate (90%) to allow some headroom for boilerplate but still catch egregiously under-tested files.

## Acceptance criteria

- A new code module with <80% coverage fails CI.
- The three currently low-coverage modules either have new tests added (preferred) or are explicitly waived with a reason.
- `creek-tools/CLAUDE.md` describes both the aggregate and per-file thresholds.

## References
- `creek-tools/pyproject.toml:74-89`
- Coverage report output
