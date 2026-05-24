# GAP-001 — Purge audit log fabricates `embeddings_removed` for embeddings that were never removed

- **Severity:** Critical
- **Prod-readiness criterion threatened:** data safety, doc honesty

## Evidence

`creek-tools/creek/purge/engine.py:687-694` (inside `_write_audit`):

```python
entry = PurgeAuditEntry(
    operation=result.operation,
    criteria=result.criteria.copy(),
    affected_fragments=result.affected_fragment_ids.copy(),
    fragments_deleted=fragments_deleted,
    references_scrubbed=result.wikilinks_removed,
    embeddings_removed=fragments_deleted,   # <-- always equals fragments_deleted
    dry_run=result.dry_run,
)
self.audit_log.append(entry)
```

The function's own docstring at `creek-tools/creek/purge/engine.py:660-664`
admits the value is fictional:

> "embeddings_removed mirrors fragments_deleted because every deleted
> fragment's cached embedding is invalidated at the next `creek link` run;
> the engine does not maintain the embedding cache directly."

The cache it claims to be invalidating is defined at
`creek-tools/creek/link/embeddings.py:50`:

```python
EMBEDDINGS_CACHE_FILENAME: Final[str] = "embeddings.parquet"
```

A search inside the purge subtree confirms purge never touches it:

```
$ grep -rn 'parquet\|embeddings' creek-tools/creek/purge/
(no results)
```

The threat model openly contradicts the README's headline RTBF claim
(`creek-tools/docs/security/threat-model.md:123-125`):

> "Wipe the embedding cache when you wipe vault content. It is *not*
> automatically purged when you `creek purge vault`. Delete the configured
> cache directory manually."

And the same doc (lines 98-100) flags embeddings as partially invertible:

> "Sentence-transformer embeddings can be partially inverted by an attacker
> who already has the cache file. Treat the cache as as-sensitive as the
> source text."

The user-facing audit-log schema example
(`creek-tools/docs/cleaning-and-purge.md:181-196`) advertises
`"embeddings_removed": 47` as a real field.

## Why it matters

The README's "Key capabilities" lists right-to-be-forgotten as *"`creek
purge` removes a fragment, source, date range, or the entire vault,
scrubbing every reference along the way."* The audit log is described one
section later (`docs/cleaning-and-purge.md:202`) as *"the system's
compliance record"* — and the docs make it explicitly **non-purgeable** so
a user can rely on it.

A compliance record that records a fabricated count for the single most
sensitive on-disk artifact (the embedding vector of an intimate-tier
fragment) is worse than no record. A user invoking purge on intimate
content believes the embedding is gone; an attacker with read access to
the vault finds the embedding still on disk, partially invertible.

## Reproduction

```bash
cd creek-tools
# scaffold a temp vault and seed two fragments
creek init --vault /tmp/gap001-vault
echo "secret intimate journal entry" > /tmp/gap001-src.md
creek ingest --type markdown --input /tmp/gap001-src.md --vault /tmp/gap001-vault
# build the embeddings cache
creek link --method embeddings --vault /tmp/gap001-vault
# confirm the parquet exists and contains rows
python -c "import pyarrow.parquet as pq; \
  t=pq.read_table('/tmp/gap001-vault/00-Creek-Meta/embeddings.parquet'); \
  print('rows:', t.num_rows)"
# pick the fragment id from 01-Fragments and purge it
FRAG=$(ls /tmp/gap001-vault/01-Fragments | head -1 | sed 's/.md$//')
creek purge fragment --id "$FRAG" --vault /tmp/gap001-vault --confirm-text "I understand this is irreversible"
# inspect the audit line
tail -1 /tmp/gap001-vault/00-Creek-Meta/audit/purge.jsonl | jq .
# observe: "embeddings_removed": 1
# now confirm the embedding row is still on disk
python -c "import pyarrow.parquet as pq; \
  t=pq.read_table('/tmp/gap001-vault/00-Creek-Meta/embeddings.parquet'); \
  print('rows after purge:', t.num_rows)"
# expected post-fix: rows after purge: 0
# actual today:      rows after purge: 1
```

Equivalent failing-test outline (`tests/test_purge.py`):

```python
def test_purge_fragment_removes_row_from_embeddings_cache(tmp_path):
    vault = _seed_vault_with_fragment_and_cache(tmp_path)
    frag_id = _read_one_fragment_id(vault)
    engine = PurgeEngine(vault_path=vault, confirmation=VAULT_PURGE_CONFIRMATION)
    engine.purge_fragment(frag_id)
    table = pq.read_table(vault / "00-Creek-Meta" / "embeddings.parquet")
    assert frag_id not in table["fragment_id"].to_pylist()
```

## Acceptance criteria

This gap is closed when **all** of the following hold:

1. After `creek purge fragment <id>`, no row with that `fragment_id`
   remains in `<vault>/00-Creek-Meta/embeddings.parquet`.
2. After `creek purge source <name>`, no row whose source frontmatter
   matches `<name>` remains in the cache.
3. After `creek purge daterange --start … --end …`, no row whose
   `computed_at` (or, better, whose source fragment's `created_at`) falls
   in the range remains in the cache.
4. After `creek purge vault`, the `embeddings.parquet` file is deleted
   (not just emptied).
5. The audit entry's `embeddings_removed` field equals the actual number
   of rows removed in that call — *zero* if the cache file did not exist
   yet, the real count otherwise.
6. A new test (`tests/test_purge.py`) verifies (1)–(4) by writing a real
   parquet cache and asserting on its post-condition row count.
7. `docs/security/threat-model.md:123-125` and
   `docs/cleaning-and-purge.md:181-196` are updated to reflect the new
   contract (either the caveat is dropped, or the audit field is renamed
   to make the deferred-invalidation behaviour explicit).

## Files affected

- `creek-tools/creek/purge/engine.py` (purge methods + `_write_audit`)
- `creek-tools/creek/purge/audit.py` (entry schema if a field renames)
- `creek-tools/creek/link/embeddings.py` (helper to delete rows by
  fragment_id, by source, by date)
- `creek-tools/tests/test_purge.py`
- `creek-tools/docs/cleaning-and-purge.md`
- `creek-tools/docs/security/threat-model.md`
- `README.md` (line 20 — consistency with the new contract)
