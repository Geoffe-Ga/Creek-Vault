# BUG-005: Ingestor errors are collected on `IngestResult.errors` but never surfaced

**Severity:** High
**Category:** BUG
**Estimated complexity:** S (≤2h)
**Parallelizable with peers in same category:** yes
**Discovered by:** Reading dimension 1 + dimension 7 — confirmed by parallel agent

## Files affected
- `creek/ingest/base.py:387, 425, 493, 512` — error collection
- `creek/pipeline.py:222-233` — error consumption

## Dependencies
None.

## Blockers
None directly, but masks failures the user needs to see.

## Reproduction
Add a malformed file to a source dir (e.g., truncated PDF, broken zip). Run `creek process`. The pipeline reports a fragment count of 0 with no indication that anything failed. Errors live only in `ingest_result.errors`, which the pipeline iterates past.

## Analysis

Each of `discover()`, `parse()`, `convert_to_markdown()`, and `generate_frontmatter()` is wrapped in `_*_safe` helpers (`creek/ingest/base.py:_discover_safe`, `_parse_safe`, `_convert_safe`, `_frontmatter_safe`). They catch `Exception`, append a string to `result.errors`, log via `logger.exception`, and return empty/None. So errors *are* logged at exception level — that's good — but the pipeline then iterates only `ingest_result.fragments`:

```python
for parsed in ingest_result.fragments:
    fragment = Fragment(...)   # see BUG-001
    fragments.append(fragment)
```

`PipelineResult` never grows an `errors` field. The CLI's `process` command prints fragment counts but no error counts. The user gets no visible signal that 47 of 48 files failed to ingest, only that "Fragments created: 1".

Combined with BUG-001 (which makes `fragments` a stub list anyway), this is double-blind.

Confidence: verified — read base.py, pipeline.py, cli.py.

## Proposed remediation

1. Add `errors: list[str] = Field(default_factory=list)` to `PipelineResult`.
2. After each ingestor call, extend `result.errors` from `ingest_result.errors` with the ingestor name prefixed for traceability.
3. CLI `process` prints both `Fragments created: N` and `Errors: M` (and `--verbose` dumps the messages).
4. Decide on a policy for non-zero error counts: probably don't fail-fast (one bad file shouldn't kill a 10k-file run), but do non-zero-exit if errors > some threshold or if `--strict` is passed.
5. Each error message must include the source path; `_*_safe` already does this, just make sure the prefix is preserved.

## Acceptance criteria

- A test creates a directory with one good file and one malformed file, runs `Pipeline.run()`, and asserts `len(result.errors) == 1` with the malformed file's path in the message.
- `creek process` prints the error count.
- `creek process --strict` exits non-zero when errors > 0.
- Re-running after fixing the bad file shows zero errors.

## References
- `creek/ingest/base.py:_discover_safe`, `_parse_safe`, `_convert_safe`, `_frontmatter_safe`
- `creek/pipeline.py:_run_ingestion`
