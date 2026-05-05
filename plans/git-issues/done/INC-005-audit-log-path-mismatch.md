# INC-005: Audit log lives at `Processing-Log/purge-log.json`, not `audit/`

**Severity:** Medium
**Category:** INC
**Estimated complexity:** S (≤2h)
**Parallelizable with peers in same category:** yes
**Discovered by:** Dimension 5; confirmed by parallel agent

## Files affected
- `creek/purge/audit.py:67-69` — log path
- `creek-tools/docs/cleaning-and-purge.md:122` — claim "Every purge writes an entry under `<vault>/00-Creek-Meta/audit/`"
- `creek-tools/docs/redaction.md:89` — claim "The audit log under `<vault>/00-Creek-Meta/audit/` records every apply"
- `creek-tools/docs/configuration.md:240` — table row "Audit log (purges, redactions) | `<vault>/00-Creek-Meta/audit/`"

## Dependencies
Pairs with SEC-005, INC-004.

## Blockers
None.

## Reproduction
```python
from pathlib import Path
from creek.purge.audit import PurgeAuditLog
log = PurgeAuditLog(Path("/tmp/v"))
print(log.log_path)
# /tmp/v/00-Creek-Meta/Processing-Log/purge-log.json
```

But the user is told to look in `/tmp/v/00-Creek-Meta/audit/`.

## Analysis

The `audit.py` module places logs under the same `Processing-Log/` directory used by the ingestion provenance log (`creek/vault/writer.py:357-358`). The docs and configuration reference table both point at `audit/`. Pick one.

Recommendation: split `Processing-Log/` (ingestion provenance) from `audit/` (purges, redactions, privacy-tier overrides) since they have different retention and integrity requirements:
- `Processing-Log/` is operational telemetry — fine to wipe, fine to be lossy.
- `audit/` is compliance — never wipe, never lossy, ideally tamper-evident (see SEC-005).

## Proposed remediation

Move the purge log to `<vault>/00-Creek-Meta/audit/purge.jsonl`. Add a redaction log at `<vault>/00-Creek-Meta/audit/redact.jsonl`. Add a privacy-override log (used by SEC-006) at `<vault>/00-Creek-Meta/audit/privacy.jsonl`. Update docs to match. Provide a one-time migration that moves any existing `purge-log.json` to the new location.

## Acceptance criteria

- New runs write to `<vault>/00-Creek-Meta/audit/purge.jsonl` (or `purge.json` if you prefer to keep JSON-array format pending SEC-005).
- An existing vault with the old path still reads correctly (migration on first run).
- All three doc files reflect the actual path.

## References
- `creek-tools/docs/cleaning-and-purge.md:122`
- `creek-tools/docs/redaction.md:89`
- `creek-tools/docs/configuration.md:240`
- `creek/purge/audit.py:67-69`
