# SEC-005: Purge audit log is not tamper-evident, locking-free, and silently rebuilds on corruption

**Severity:** High
**Category:** SEC
**Estimated complexity:** M (≤1d)
**Parallelizable with peers in same category:** yes
**Discovered by:** Reading dimension 2 + 7 — `creek/purge/audit.py`

## Files affected
- `creek/purge/audit.py` — entire module
- `creek-tools/docs/cleaning-and-purge.md:122-138` — claim "append-only" + JSON shape with `affected_fragments`, `fragments_deleted`, `references_scrubbed`, `embeddings_removed`, `operator: sgsg`

## Dependencies
None. Should be fixed alongside INC-004 (audit log fields don't match docs) and INC-005 (path mismatch).

## Blockers
The "append-only" claim is also referenced in `docs/redaction.md:89` ("audit log under `<vault>/00-Creek-Meta/audit/` records every apply"). Any user relying on the audit log as a compliance record gets none of the guarantees it advertises.

## Reproduction
```python
from creek.purge.audit import PurgeAuditLog, PurgeAuditEntry
from pathlib import Path
log = PurgeAuditLog(Path("/tmp/v"))
Path("/tmp/v/00-Creek-Meta/Processing-Log").mkdir(parents=True)
log.append(PurgeAuditEntry(operation="fragment", target="frag-A", count=1))
# Now corrupt the log
log.log_path.write_text("not json")
log.append(PurgeAuditEntry(operation="fragment", target="frag-B", count=1))
print(log.read())
# [PurgeAuditEntry(operation='fragment', target='frag-B', count=1, ...)]
# The first entry is silently lost.
```

## Analysis

Three independent integrity defects:

1. **Read-modify-write, no locking.** `append()` calls `_read_entries()` then re-serialises the whole list. Two concurrent appends race; one is lost. No `fcntl.flock` or equivalent.
2. **Silent rebuild on malformed JSON.** `_read_entries` (lines 113-118) catches `json.JSONDecodeError`/`OSError`, logs a warning, returns `[]`. The next `append` then writes a single-entry log over the corrupt one — destroying every prior entry. This is the *opposite* of an audit log's job.
3. **No integrity hash / signature.** A user (or attacker, but mostly: a careless operator) can edit the JSON file with any text editor and remove entries. There's no detection.

Plus the "append-only" docstring (line 8) is itself misleading — the implementation is read-modify-write.

Plus, per INC-004 / INC-005:
- Path is `<vault>/00-Creek-Meta/Processing-Log/purge-log.json`; docs say `<vault>/00-Creek-Meta/audit/`.
- Schema is `{operation, target, count, operator, dry_run, timestamp}`; docs say `{operation, criteria, affected_fragments[], operator, timestamp, fragments_deleted, references_scrubbed, embeddings_removed}`.
- Operator default is `"human via CLI"`; docs example uses `"sgsg"`.
- There is *no* audit log for the redaction module (`creek/redact/`) at all, despite `docs/redaction.md:89` claiming one.

Confidence: verified.

## Proposed remediation

1. **Switch to JSONL with O_APPEND.** One entry per line; POSIX `O_APPEND` writes are atomic for line-sized payloads. Eliminates the read-modify-write race and removes the corrupt-rebuild path.
2. **Hash chain.** Each entry includes `prev_hash: sha256(previous_line)`. Tampering with any entry invalidates the chain; an integrity-check command can verify the log.
3. **Match the spec schema.** Add `affected_fragments`, `fragments_deleted`, `references_scrubbed`, `embeddings_removed` fields. Decide whether to actually populate them from the engine's results.
4. **Add a redaction audit log.** Either reuse the same logger, or write a parallel `redact-log.jsonl` next to it. Document the path.
5. **Move the directory** to `<vault>/00-Creek-Meta/audit/` to match the docs (or update the docs — but the spec calls for the audit dir, so move).
6. **Tighter operator handling.** Read from `$USER` / `getpass.getuser()`; refuse the default `"human via CLI"` if the env is unset only when `--operator` is also unset.

## Acceptance criteria

- Concurrent appends from N processes produce N entries, no losses (validated via threaded test).
- Manual corruption of the log file is detected by a verification command (`creek audit verify`) rather than silently rebuilt.
- Log entries match the schema documented in `docs/cleaning-and-purge.md`.
- `creek redact --apply` writes an entry to the redaction audit log on every committed change.
- The log lives at `<vault>/00-Creek-Meta/audit/` (or the docs are explicit about a different path).

## References
- `creek/purge/audit.py`
- `creek-tools/docs/cleaning-and-purge.md:122-138`
- `creek-tools/docs/redaction.md:89`
- `00-Creek-Meta/Ontology/creek_ontology_agent_prompt.md` §8.3 (provenance log "must be append-only")
