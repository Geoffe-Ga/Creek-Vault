# OPS-003: Log messages lack fragment IDs / source paths needed to triage failures

**Severity:** Medium
**Category:** OPS
**Estimated complexity:** S (≤2h)
**Parallelizable with peers in same category:** yes
**Discovered by:** Dimension 7

## Files affected
- `creek/classify/llm.py:628-632` — exhausted retries log
- `creek/classify/llm.py:658-662` — batch-failure log
- `creek/redact/scanner.py:16` (and broader) — scanner logs
- General pattern across modules

## Dependencies
None.

## Reproduction
Sample log line for a retry-exhausted classification:
```
ERROR creek.classify.llm: All 5 retries exhausted for 'A note on attention'
```

The fragment title is included; the fragment ID is not. With 10k fragments, several share the same auto-generated title. Investigators have nothing to grep for.

## Analysis

The pattern across the modules is "log the user-facing string." For triage at scale, logs need stable identifiers:
- Fragment ID (deterministic, unique)
- Source file path (where applicable)
- Operation (classify/ingest/redact/etc.)
- Provider name (ollama/anthropic) where applicable

Plus consistent log levels:
- `INFO` for stage transitions
- `WARNING` for recoverable issues (retry, fallback used)
- `ERROR` for unrecoverable per-fragment failures
- `EXCEPTION` for unhandled exceptions (already done correctly via `logger.exception`)

The current code mixes these.

## Proposed remediation

Adopt structured logging with `extra={"fragment_id": ..., "source_path": ..., ...}`. Even with the default formatter, fragment IDs and paths in the message string are a big upgrade. Suggested format:

```
ERROR creek.classify.llm [fragment=frag-abc123 path=/exports/journal/2025-01-01.md provider=anthropic] retries exhausted (5)
```

Sweep `creek/classify/`, `creek/ingest/`, `creek/redact/`, `creek/purge/` for log calls that don't include an identifier. Add one.

## Acceptance criteria

- Every error log line for a per-fragment failure includes the fragment ID.
- Every error log line for a per-file failure includes the file path.
- A documented logging convention lives in `creek-tools/docs/operations.md` (new file, also covers OPS-001 resume).
- An e2e test that triggers a per-fragment failure asserts the fragment ID appears in stderr.

## References
- `creek/classify/llm.py:628-632, 658-662, 696-699`
- `creek/redact/scanner.py`
- `creek/ingest/base.py:_*_safe`
