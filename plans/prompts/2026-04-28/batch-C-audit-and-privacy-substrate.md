# Batch C — Audit and privacy substrate

## Role

You are a security and reliability engineer responsible for the system's compliance record. You design append-only logs that survive crashes and hostile editors, you treat privacy-tier filters as enforcement boundaries (not advisory hints), and you understand that a tampered audit log is worse than no audit log.

## Goal

Rebuild the audit logging substrate as tamper-evident JSONL with hash-chain integrity, locking, and the documented schema; wire redaction-apply, purge operations, and privacy-tier overrides into it; make `creek mine` and `creek draft` actually filter `privacy_tier=intimate` fragments by default; expose the `--include-tier` CLI flag with audit-logged overrides.

By the end of this batch, every privacy or compliance claim in `docs/redaction.md`, `docs/cleaning-and-purge.md`, and `docs/generation.md` matches the implementation.

## Context

This batch is the system's compliance backbone. Today: the purge audit log silently drops entries when the JSON is corrupt, races on concurrent writes, has a schema that doesn't match the docs, lives at the wrong path, and has no equivalent for redaction or privacy-override events. Meanwhile, `creek mine` and `creek draft` happily feed intimate journal/recovery content to whatever LLM the user has configured — the headline privacy guarantee fails silently.

The batch's seven issues all touch the audit/privacy plane and are best done as one coherent change because they share data structures, paths, and tests.

**Independent of Batches A and B** — can run in parallel, but the privacy-tier filter (SEC-006) is more useful once Batch B's `creek mine` / `creek draft` are wired to real engines. Coordinate landing order with Batch B.

**Read these issue files before starting** (in `plans/git-issues/`):
- `SEC-005-audit-log-not-tamper-evident.md` — JSONL + hash chain + locking
- `INC-004-audit-log-schema-mismatch.md` — `affected_fragments`, `fragments_deleted`, `references_scrubbed`, `embeddings_removed`
- `INC-005-audit-log-path-mismatch.md` — move from `Processing-Log/` to `audit/`
- `INC-015-no-redaction-audit-log.md` — `creek redact --apply` writes no audit
- `SEC-006-mine-and-draft-do-not-filter-intimate.md` — privacy filter in generation
- `INC-007-include-tier-cli-flag-missing.md` — CLI flag with audit override
- `PERF-002-provenance-and-audit-log-rewrite.md` — full-rewrite-on-every-append performance hit

**Files you will primarily change:**
- `creek-tools/creek/purge/audit.py` — extend or replace `PurgeAuditLog`
- `creek-tools/creek/redact/redactor.py`, `creek/redact/cli_commands.py` — write audit entries on `--apply`
- `creek-tools/creek/generate/mining.py`, `creek/generate/drafts.py` — privacy-tier filter
- `creek-tools/creek/cli.py` — `--include-tier` flag on `mine`, `draft`, `report`, `skills`
- `creek-tools/creek/vault/writer.py` — `_log_provenance` becomes JSONL with O_APPEND (PERF-002)

**Files to consult (do not redesign):**
- `creek/classify/privacy.py` — `PrivacyClassifier.enforce_tier`
- `creek/generate/voice.py:147-160` — the one place tier filtering already works (use as model)
- `creek/models.py` — `PrivacyTier` enum

## Output format

A logical commit sequence:

1. **Audit JSONL infrastructure.** Introduce `AuditLog` (or rework `PurgeAuditLog`) supporting JSONL with `O_APPEND`, hash chain, file locking (`fcntl.flock` on POSIX), and a `verify()` method. Drop the read-modify-write pattern.
2. **Migrate purge log.** `PurgeAuditEntry` adopts the documented schema (`affected_fragments`, `fragments_deleted`, `references_scrubbed`, `embeddings_removed`, `criteria` dict). New entries go to `<vault>/00-Creek-Meta/audit/purge.jsonl`. Read path tolerates the legacy `Processing-Log/purge-log.json` shape for one release.
3. **Migrate provenance log.** `VaultWriter._log_provenance` uses the same JSONL primitive. Path stays under `Processing-Log/` (operational, not compliance).
4. **Add redaction audit.** `creek redact --apply` writes one entry per touched file to `<vault>/00-Creek-Meta/audit/redact.jsonl` with file path, pattern names, match counts, replacement template, dry-run flag.
5. **Privacy filter in generation.** `creek/generate/mining.py:_load_fragments` and `creek/generate/drafts.py:_load_fragments_by_id` skip `privacy_tier == intimate` by default; replace `personal` bodies with summaries (title-only is fine for v1).
6. **`--include-tier` flag.** Add to `mine`, `draft`, `report`, `skills`. Default behaviour preserves tier filtering. When the flag elevates inclusion, write a `<vault>/00-Creek-Meta/audit/privacy.jsonl` entry with operator, command, fragment IDs, timestamp.

## Examples

The audit-log integrity test that should pass at the end of this batch:

```python
def test_audit_log_detects_tampering(tmp_path):
    log = AuditLog(tmp_path / "audit.jsonl")
    log.append({"operation": "purge.fragment", "target": "frag-abc", "count": 1})
    log.append({"operation": "purge.fragment", "target": "frag-def", "count": 1})

    # Hostile edit: remove the first entry
    lines = log.path.read_text().splitlines()
    log.path.write_text(lines[1] + "\n")

    with pytest.raises(AuditChainBroken):
        log.verify()


def test_audit_log_concurrent_appends_lose_nothing(tmp_path):
    log = AuditLog(tmp_path / "audit.jsonl")
    def append_n(i):
        for j in range(100):
            log.append({"op": "x", "i": i, "j": j})
    with ThreadPoolExecutor(max_workers=8) as ex:
        list(ex.map(append_n, range(8)))
    assert sum(1 for _ in log.read()) == 800   # nothing lost
```

The privacy filter test:

```python
def test_mine_excludes_intimate_by_default(tmp_path):
    vault = make_vault_with_fragments(tmp_path, [
        Fragment(..., privacy_tier="intimate"),
        Fragment(..., privacy_tier="open"),
    ])
    seeds = IdeaMiner(vault_path=vault).mine_all()
    assert all(s.fragment.privacy_tier != "intimate" for s in seeds)


def test_mine_with_include_tier_writes_audit(tmp_path):
    ...
    runner.invoke(app, ["mine", "--vault", str(vault), "--include-tier", "intimate"])
    entries = list(AuditLog(vault / "00-Creek-Meta" / "audit" / "privacy.jsonl").read())
    assert any(e["fragment_ids"] for e in entries)
```

## Requirements

- **Use `/stay-green`** with the integrity tests above as Gate 1 — they must fail before the new code lands and pass once it does.
- **Use `/max-quality-no-shortcuts`** if you're tempted to keep a "rebuild on corruption" branch that silently drops history (the current `_read_entries` does this — remove it). Corruption must raise.
- The hash chain is `prev_hash = sha256(previous_line_bytes)` — no fancy crypto. The threat model is "careless or hostile editor", not "well-funded adversary"; HMAC with a vault-local secret is overkill.
- `O_APPEND` writes for log lines under ~1 KiB are atomic on POSIX — use that. On Windows, document the limitation and use `fcntl` equivalents or `portalocker` if you want cross-platform.
- The migration path for legacy `purge-log.json` is one-way: read once, rewrite to the new JSONL location, log the migration, then delete the old file. Test the migration explicitly.
- Privacy-tier filtering must be a single helper used by every generation flow; do not scatter `if tier == "intimate"` across modules. The helper lives near `creek/classify/privacy.py` for discoverability.
- The `--include-tier` flag accepts `open`, `personal`, `intimate`, `all` (use a `Literal` or Typer `Enum`). Default behaviour does not require the flag.
- Maintain `mypy --strict` clean. The new `AuditLog` API should be fully typed.
- Maintain ≥90% branch coverage. The audit log module should be at ≥95% — it's a security-critical surface.
- Update `docs/cleaning-and-purge.md`, `docs/redaction.md`, `docs/generation.md`, `docs/configuration.md` so the path/schema/flag claims match reality.

## Definition of done

`./scripts/check-all.sh` exits 0. The integrity tests pass. A user can run `creek redact --apply`, `creek purge fragment`, `creek purge source`, `creek mine --include-tier intimate`, and observe a complete, tamper-evident audit trail at `<vault>/00-Creek-Meta/audit/`. `creek mine` and `creek draft` without `--include-tier` produce no intimate content in their output.
