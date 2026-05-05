# INC-004: Purge audit log schema does not match documented JSON shape

**Severity:** High
**Category:** INC
**Estimated complexity:** S (≤2h)
**Parallelizable with peers in same category:** yes
**Discovered by:** Dimension 5; confirmed by parallel agent

## Files affected
- `creek/purge/audit.py:29-49` — `PurgeAuditEntry`
- `creek-tools/docs/cleaning-and-purge.md:122-138` — example JSON

## Dependencies
Should be fixed alongside SEC-005 (audit log integrity) and INC-005 (path mismatch).

## Blockers
None.

## Reproduction
Look at the implementation:
```python
class PurgeAuditEntry(BaseModel):
    timestamp: str
    operation: str
    target: str
    count: int
    operator: str
    dry_run: bool
```

vs. the documented shape:
```json
{
  "operation": "purge.source",
  "criteria": {"source_path": "/home/me/exports/unwanted.zip"},
  "affected_fragments": ["frag-...", "frag-..."],
  "operator": "sgsg",
  "timestamp": "2026-04-28T18:01:23Z",
  "fragments_deleted": 47,
  "references_scrubbed": 312,
  "embeddings_removed": 47
}
```

Differences: `criteria` (structured) vs `target` (string). `affected_fragments` (list) — missing. `fragments_deleted` (count) — represented as `count` in the model. `references_scrubbed` — missing entirely. `embeddings_removed` — missing entirely. `operator` example uses `sgsg`; default value is `"human via CLI"`.

## Analysis

The model captures only the *minimum* information; the docs promise a richer record that would actually support compliance audit ("47 fragments deleted, 312 references scrubbed, 47 embeddings removed"). The engine *does* compute these counts (`creek/purge/engine.py:_decrement_counts`, `_purge_single`) — they're just not propagated to the audit log.

For a system that frames itself around right-to-be-forgotten, the audit record needs to actually verify the right was honoured. Without `references_scrubbed` and `embeddings_removed`, a user has no proof that purge was complete — and per spec §8.3 the audit trail "must be append-only" precisely so it serves that role.

Confidence: verified.

## Proposed remediation

Extend `PurgeAuditEntry`:

```python
class PurgeAuditEntry(BaseModel):
    timestamp: str
    operation: str
    criteria: dict[str, Any]
    affected_fragments: list[str]
    fragments_deleted: int
    references_scrubbed: int
    embeddings_removed: int
    operator: str
    dry_run: bool
```

Update `PurgeEngine._write_audit` to populate every field from the `PurgeResult`. Remove the legacy `target` and `count` fields, or keep them as aliases that degrade gracefully when older logs are read.

For backward compatibility, the read path (`PurgeAuditLog.read`) should accept both old and new shapes for one release.

## Acceptance criteria

- A test runs `purge fragment` against a fragment that has 7 inbound references, asserts the audit entry contains `affected_fragments: ["frag-..."]`, `fragments_deleted: 1`, `references_scrubbed: 7`, `embeddings_removed: 1`.
- Reading an older `target+count`-shaped log entry does not crash.
- The example JSON in `docs/cleaning-and-purge.md` is reproduced byte-for-byte (modulo timestamp/operator/IDs) by an actual run.

## References
- `creek-tools/docs/cleaning-and-purge.md:122-138`
- `creek/purge/audit.py:29-49`
- `creek/purge/engine.py` (where the counts come from)
