# Cleaning and purge

Two adjacent command groups handle vault hygiene:

- **`creek clean`** — non-destructive maintenance: orphans, stale reviews, broken links, duplicates.
- **`creek purge`** — destructive deletion (right-to-be-forgotten): fragments, sources, classifications, date ranges, the whole vault.

`clean` finds problems and reports them. `purge` deletes things. They're separate tools because mistaking one for the other would be expensive.

---

## Cleaning

### `creek clean orphans`

Identifies fragments with **zero incoming and outgoing links** that are older than N days. These are typically failed ingestions, single-shot notes that never connected to anything, or fragments whose links pointed to since-deleted siblings.

```bash
creek clean orphans --vault ~/Obsidian/Creek-Vault --age-days 30
```

Output is a markdown report — orphans are not deleted automatically. Pipe the IDs into `creek purge fragment` if you want them gone.

### `creek clean stale-reviews`

Surfaces fragments that have been sitting in the review queue for longer than N days. Helps catch the long tail of "I'll classify that later".

```bash
creek clean stale-reviews --vault ~/Obsidian/Creek-Vault --age-days 14
```

### `creek clean broken-links`

Scans every fragment for wiki-links pointing to nonexistent files. Common causes: a target was renamed, a target was purged, a typo.

```bash
creek clean broken-links --vault ~/Obsidian/Creek-Vault
```

The report groups broken links by source fragment so you can fix them in batches.

### `creek clean duplicates`

Runs the dedup sweep (`creek.clean.dedup`) and emits a review report. Two strategies are layered:

1. **Normalized exact-match** — strips whitespace and frontmatter, then compares hashes. Catches multiple ingests of the same export.
2. **Embedding-based near-duplicates** — flags fragments whose cosine similarity exceeds `DeduplicationConfig.semantic_threshold`. Catches paraphrased reposts.

```bash
creek clean duplicates --vault ~/Obsidian/Creek-Vault
```

Like orphans, dups are reported, not auto-deleted — you decide which copy to keep.

### `creek clean report`

Aggregate health summary: fragment count, orphan rate, broken-link rate, mean fan-out, review-queue depth, recent classification accuracy. Useful as a weekly check-in.

```bash
creek clean report --vault ~/Obsidian/Creek-Vault
```

---

## Purge (right-to-be-forgotten)

`creek purge` is destructive. Every command:

1. **Refuses without confirmation** unless you pass `--yes`.
2. **Records an audit entry** by appending one JSONL line to `<vault>/00-Creek-Meta/audit/purge.jsonl` with the criteria, the affected fragment IDs, and the operator. The file is hash-chained — see [Audit trail](#audit-trail) for the integrity guarantees.
3. **Scrubs every reference** — wiki-links pointing at the deleted fragment(s) are removed from every other fragment's frontmatter and body.
4. **Removes embeddings** from the cache at the next `creek link` run so the deleted content is no longer surfaceable through resonances.

### `creek purge fragment`

Delete a single fragment by ID:

```bash
creek purge fragment --id frag-9c1f3a2b8e02 --vault ~/Obsidian/Creek-Vault
```

Use this when you've already identified the unwanted fragment (e.g. via `creek clean orphans`).

### `creek purge source`

Delete every fragment ingested from a given source path. Useful when you imported a directory by accident, or when a person has asked for their messages to be removed.

```bash
creek purge source --source-path ~/exports/unwanted.zip --vault ~/Obsidian/Creek-Vault
```

The source path is matched against `source.original_file` in each fragment's frontmatter — exact match by default, or substring with `--match substring`.

### `creek purge classifications`

Resets every fragment's classification fields to `unclassified` without deleting the fragments themselves. Run this after editing the keyword atlas if you want a clean slate before re-classifying:

```bash
creek purge classifications --vault ~/Obsidian/Creek-Vault
creek classify --vault ~/Obsidian/Creek-Vault --method rules --force
```

### `creek purge daterange`

Delete every fragment created within a date range. The range is inclusive on both ends and matches `ingested` timestamps:

```bash
creek purge daterange --start 2025-01-01 --end 2025-01-31 --vault ~/Obsidian/Creek-Vault
```

### `creek purge vault`

Nuclear option: destroys every fragment, thread, eddy, and resonance. Leaves the directory structure and `00-Creek-Meta/` intact. This is **never** undoable; it requires `--yes` *and* an interactive prompt.

```bash
creek purge vault --vault ~/Obsidian/Creek-Vault --yes
```

---

## Audit trail

Every purge appends one JSONL line to `<vault>/00-Creek-Meta/audit/purge.jsonl`. Each entry carries an inline `prev_hash` (sha256 of the previous line) so a tampered or truncated log can be detected via the verification API:

```json
{
  "operation": "source",
  "criteria": {"source_type": "claude"},
  "affected_fragments": ["frag-...", "frag-..."],
  "operator": "sgsg",
  "timestamp": "2026-04-28T18:01:23Z",
  "fragments_deleted": 47,
  "references_scrubbed": 312,
  "embeddings_removed": 47,
  "dry_run": false,
  "prev_hash": "0000…"
}
```

`creek redact --apply` writes alongside it at `<vault>/00-Creek-Meta/audit/redact.jsonl`. Privacy-tier overrides (e.g. `creek mine --include-tier intimate`) write to `<vault>/00-Creek-Meta/audit/privacy.jsonl`. Operational provenance from ingestion stays at `<vault>/00-Creek-Meta/Processing-Log/provenance.jsonl` (separate location: not compliance-grade, allowed to be lossy).

A pre-Batch-C `Processing-Log/purge-log.json` from older installs is migrated automatically on first read or write — every legacy entry is replayed into the new chain, a `purge.audit.migration` marker is recorded, and the old file is removed.

The audit trail itself is **not purgeable** by `creek` — it's the system's compliance record. You can `git rm` it manually, but that's outside the tool.

## Recovery

`creek purge` works at the filesystem level — fragments are deleted from `01-Fragments/`. If you ran the wrong purge:

- **If you have a clean git working tree**: `git status` will show the deletions; `git checkout -- 01-Fragments/` restores them. Re-run `creek link` to rebuild references.
- **If you've already committed**: `git revert` or `git restore --source HEAD~1`.
- **If you haven't been committing** (don't): the only recovery is to re-ingest from the original sources.

This is one of several reasons the top-level repo is git-tracked.
