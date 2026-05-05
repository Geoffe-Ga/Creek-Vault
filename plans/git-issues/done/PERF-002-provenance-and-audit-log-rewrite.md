# PERF-002: Provenance and audit logs are rewritten in full on every append

**Severity:** High
**Category:** PERF
**Estimated complexity:** S (≤2h)
**Parallelizable with peers in same category:** yes
**Discovered by:** Dimension 9; closely related to SEC-005

## Files affected
- `creek/vault/writer.py:341-377` — `_log_provenance`
- `creek/purge/audit.py:71-87` — `PurgeAuditLog.append`

## Dependencies
Pairs with SEC-005 (audit integrity) — both want to switch to JSONL.

## Blockers
None for small N; severe past 1k log entries.

## Reproduction
Each call to `_log_provenance` reads the full JSON, deserialises, appends one dict, re-serialises with `indent=2`, writes. For a 10k-fragment ingestion, that's 10k reads of an ever-growing JSON file. Sample timing on a 10MB log: ~100ms per write × 10k writes = 17 minutes of pure log overhead.

The same shape is used by `PurgeAuditLog.append`, so a `purge` of 1000 fragments performs 1000 read-modify-write cycles on the same file.

## Analysis

```python
# vault/writer.py:_log_provenance
entries: list[dict[str, str]] = []
if log_path.exists():
    raw = log_path.read_text(encoding="utf-8")
    if raw.strip():
        entries = json.loads(raw)
entry: dict[str, str] = {...}
entries.append(entry)
log_path.write_text(json.dumps(entries, indent=2), encoding="utf-8")
```

Beyond the obvious wall-clock cost, this pattern:
- Loses every concurrent write (BUG-006 race).
- Risks file truncation if the process crashes mid-write (no fsync, no temp+rename).
- Doubles disk I/O linearly with log size.

Confidence: verified.

## Proposed remediation

Convert both logs to JSONL (one entry per line). Open with `O_APPEND`. Each append is a single atomic `write()` for line-sized payloads on POSIX. Reading the log iterates lines and `json.loads` each one. Memory stays bounded.

For the read-side helpers (e.g., `PurgeAuditLog.read`), preserve the API but back it with `for line in open(path): yield json.loads(line)`.

Once JSONL is in place, the SEC-005 hash-chain remediation slots in cleanly: each line includes `prev_hash` of the previous line.

## Acceptance criteria

- Per-append wall time is constant in N (median).
- Concurrent appends from N processes produce N entries with no losses (single test).
- Process kill mid-append corrupts at most one line; the rest remain readable.
- Read API returns the same shape as before.

## References
- `creek/vault/writer.py:341-377`
- `creek/purge/audit.py:71-87`
- SEC-005, BUG-006
