# GAP-002 — `creek purge vault` has no transactional protection and writes its audit *after* destruction

- **Severity:** Critical
- **Prod-readiness criterion threatened:** data safety, crash recovery

## Evidence

`creek-tools/creek/purge/engine.py:380-397` (`purge_vault`):

```python
result = PurgeResult(
    operation="vault",
    target="entire vault",
    criteria={"scope": "entire vault"},
    dry_run=self.dry_run,
)
for folder in _VAULT_CONTENT_FOLDERS:
    folder_path = self.vault_path / folder
    if not folder_path.is_dir():
        continue
    self._wipe_folder_contents(folder_path, result)
result.fragments_affected = len(result.deleted_files)
self._write_audit(result)   # <-- audit only after the loop completes
return result
```

`_wipe_folder_contents` (lines 646-653):

```python
for entry in sorted(folder_path.iterdir()):
    result.deleted_files.append(str(entry))
    if self.dry_run:
        continue
    if entry.is_dir():
        shutil.rmtree(entry)
    else:
        entry.unlink()
```

There is no staging directory, no rename-to-trash, no fsync barrier, no
pre-destruction journal entry. If `shutil.rmtree` or `entry.unlink` raises
mid-loop (read-only file, permission error, OOM, container reclamation,
Ctrl-C), every entry already unlinked is permanently gone, every entry
remaining is intact, and the audit log records *nothing*.

`creek-tools/tests/test_purge.py` (1547 lines, 63 test functions) does
cover audit-log migration OSError
(`test_audit_log_migration_oserror_preserves_legacy_and_logs`, line 1378)
and orphaned legacy logs, but no test simulates a crash inside
`_wipe_folder_contents` or `_scrub_wikilinks`. Search:

```
$ grep -n "_wipe_folder_contents\|side_effect=OSError" tests/test_purge.py
# Only `_audit_path` / `_path_resolution` OSErrors are covered.
```

## Why it matters

Crash recovery was one of the four named prod-readiness criteria. The
single most destructive command in the system has no recovery story —
and the audit log that the docs (`docs/cleaning-and-purge.md:179-202`)
position as *"the system's compliance record"* is the only artifact the
user has to reconstruct what was destroyed. That artifact does not exist
on the crash path.

For a vault holding 5 years of intimate-tier journal entries, a SIGKILL
mid-purge produces a non-recoverable, non-auditable, indeterminate state.

## Reproduction

Steps:

```bash
cd creek-tools
creek init --vault /tmp/gap002-vault
# seed multiple content folders
mkdir -p /tmp/gap002-vault/01-Fragments /tmp/gap002-vault/02-Threads \
         /tmp/gap002-vault/03-Eddies /tmp/gap002-vault/04-Praxis
echo "f" > /tmp/gap002-vault/01-Fragments/a.md
echo "t" > /tmp/gap002-vault/02-Threads/b.md
echo "e" > /tmp/gap002-vault/03-Eddies/c.md
echo "p" > /tmp/gap002-vault/04-Praxis/d.md
# make 03-Eddies unwriteable so rmtree fails mid-loop
chmod 0500 /tmp/gap002-vault/03-Eddies
creek purge vault --vault /tmp/gap002-vault \
    --confirm-text "I understand this is irreversible" --force-non-interactive
# observe:
#   01-Fragments/a.md gone, 02-Threads/b.md gone
#   03-Eddies/c.md still present (rmtree failed)
#   04-Praxis/d.md still present (loop aborted)
#   00-Creek-Meta/audit/purge.jsonl: NO new entry
```

Failing-test outline:

```python
def test_purge_vault_audits_partial_destruction(tmp_path, monkeypatch):
    vault = _seed_vault_with_two_folders(tmp_path)
    engine = PurgeEngine(vault_path=vault, confirmation=VAULT_PURGE_CONFIRMATION)
    # Make the second folder's rmtree raise to simulate a mid-loop crash.
    real_rmtree = shutil.rmtree
    call_count = {"n": 0}
    def flaky_rmtree(path, *a, **kw):
        call_count["n"] += 1
        if call_count["n"] == 2:
            raise OSError("simulated mid-purge")
        return real_rmtree(path, *a, **kw)
    monkeypatch.setattr(shutil, "rmtree", flaky_rmtree)
    with pytest.raises(OSError):
        engine.purge_vault(VAULT_PURGE_CONFIRMATION)
    # Today: 0 entries. Acceptance: at least one "intent" entry exists.
    audit = (vault / "00-Creek-Meta" / "audit" / "purge.jsonl").read_text().splitlines()
    assert any(json.loads(l).get("phase") == "intent" for l in audit)
```

## Acceptance criteria

Closed when **all** hold:

1. `purge_vault` writes an *intent* audit entry **before** the first
   destructive operation, capturing the scope (vault path, folders to
   wipe, dry-run flag) and a fresh `operation_id`.
2. On successful completion, an *outcome* entry sharing that
   `operation_id` is appended with the actual count and the matching
   hash-chain `prev_hash`.
3. On exception during the wipe loop, the exception propagates **and**
   the outcome entry records `status="partial"` with whichever files
   were already collected in `result.deleted_files` before the failure.
   The intent entry has already been written, so a crash before the
   outcome entry still leaves a trace.
4. Either the actual deletion uses a rename-to-staging pattern
   (e.g. `<vault>/.creek-purge-<operation_id>/`) followed by a single
   `rmtree` at the end, or the operation documents (in code + docs) that
   it is not transactional and recovery is operator-managed via the
   intent log. Renaming is strongly preferred.
5. New tests cover: (a) intent line exists before any destructive op
   runs; (b) outcome line records `status="partial"` on injected
   OSError; (c) the staging directory (if used) is cleaned even on
   exception.

## Files affected

- `creek-tools/creek/purge/engine.py` (`purge_vault`, every other
  `_write_audit` call — see GAP-010 for the same root-cause across
  per-fragment / per-source / per-daterange paths)
- `creek-tools/creek/purge/audit.py` (entry schema gets a
  `phase: intent|outcome` discriminator and `operation_id`)
- `creek-tools/tests/test_purge.py`
- `creek-tools/docs/cleaning-and-purge.md` (audit-trail section
  documents intent/outcome semantics)

## Dependencies / blockers

Tightly coupled to GAP-010 (the same after-destruction audit pattern
exists in every purge op, not just `purge_vault`). Fixing GAP-002
without GAP-010 leaves the per-fragment case still vulnerable, but the
schema change underlying GAP-002 (phase + operation_id) is the same
shape that GAP-010 needs. Solve in one PR.
