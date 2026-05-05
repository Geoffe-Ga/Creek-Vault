# INC-015: `creek redact --apply` does not write to any audit log

**Severity:** High
**Category:** INC
**Estimated complexity:** S (≤2h)
**Parallelizable with peers in same category:** yes
**Discovered by:** Dimension 5

## Files affected
- `creek/redact/redactor.py` — no audit-log calls
- `creek/redact/cli_commands.py` — no audit-log calls
- `creek-tools/docs/redaction.md:89` — claim "The audit log under `<vault>/00-Creek-Meta/audit/` records every apply"

## Dependencies
SEC-005 (audit log integrity), INC-005 (audit path), INC-004 (audit schema).

## Reproduction
```bash
grep -rn "audit\|AuditLog" creek/redact/   # zero hits
```

`creek redact --apply` modifies source files in place; nothing records that fact in a structured audit. The `creek/purge/audit.py` log exists but only `purge` uses it.

## Analysis

`docs/redaction.md` line 89:
> **Audit** the report monthly. The audit log under `<vault>/00-Creek-Meta/audit/` records every apply.

There is no such log. Even the per-source `<source>/.creek-redactions/queue.json` only tracks pattern matches and offsets, not the full audit-grade record (operator, timestamp, what was replaced with what, file checksums before/after). A user trying to verify "did we redact every personal email last month?" has nothing to query.

For a system whose primary security property is "scan and redact secrets before they enter the vault," the absence of an audit trail for the apply operation is a meaningful gap.

Confidence: verified.

## Proposed remediation

Add a `RedactionAuditLog` mirroring `PurgeAuditLog` (or share the JSONL infrastructure once SEC-005 lands). Each apply writes one entry per file containing:
- timestamp
- source path (relative to source root)
- pattern names that matched
- match counts per pattern
- operator
- dry-run flag
- replacement template used

Persist at `<vault>/00-Creek-Meta/audit/redact.jsonl` (per INC-005).

## Acceptance criteria

- Every `creek redact --apply` invocation appends entries (one per touched file) to the redaction audit log.
- `--dry-run` writes entries with `dry_run: true`; the source files are not modified.
- The audit log is readable via the same `audit_log.read()` API as the purge log (or a parallel one).
- Tests verify entry shape and content.

## References
- `creek-tools/docs/redaction.md:89`
- `creek/redact/`
- INC-004, INC-005, SEC-005
