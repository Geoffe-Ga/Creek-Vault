# Cleaning and purge

Two adjacent command groups handle vault hygiene:

- **`creek clean`** — non-destructive maintenance: orphans, stale reviews, broken links, duplicates.
- **`creek purge`** — destructive deletion (right-to-be-forgotten): fragments, sources, classifications, date ranges, the whole vault.

`clean` finds problems and reports them. `purge` deletes things. They're separate tools because mistaking one for the other would be expensive.

> Purge does best-effort deletion. It is **not** anti-forensic — modern SSDs and copy-on-write filesystems retain old block contents even after a successful unlink. See the [threat model](security/threat-model.md) for the full set of guarantees Creek does and doesn't make.

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
3. **Scrubs every reference** — wiki-links pointing at the deleted fragment(s) are removed from every other `.md` file in the vault (matched by title), and every word-boundary mention of the deleted fragment ID is replaced with the `[purged]` placeholder across YAML frontmatter (e.g. `source_fragments: […]` in drafts and mining ideas) and body text. The walk covers `04-Praxis/`, `05-Wavelength/`, `06-Frequencies/`, `07-Voice/Drafts/`, `08-Decisions/`, `09-Reference/`, `10-Liminal/`, and `00-Creek-Meta/Skills/` (deployed skill tree). The compliance audit log itself (`00-Creek-Meta/audit/purge.jsonl`) is JSONL, not Markdown, and is intentionally excluded — `affected_fragments` in audit entries keeps the real ID for forensic reconstruction (GAP-004).
4. **Removes embeddings** from `<vault>/00-Creek-Meta/embeddings.parquet` (GAP-001). Per-fragment / per-source / per-date-range purges drop matching rows; `creek purge vault` deletes the file outright.

### `creek purge fragment`

Delete a single fragment by ID:

```bash
creek purge fragment --id frag-9c1f3a2b8e02 --vault ~/Obsidian/Creek-Vault
```

Use this when you've already identified the unwanted fragment (e.g. via `creek clean orphans`).

### `creek purge source`

Delete every fragment ingested from a given source. Two complementary modes (INC-008):

- **By platform** (positional argument). Matches against `source.platform`. Useful when you want to wipe everything from a specific exporter:

  ```bash
  creek purge source claude --vault ~/Obsidian/Creek-Vault
  creek purge source discord --vault ~/Obsidian/Creek-Vault
  ```

- **By source path** (`--source-path`). Matches against `source.original_file` in each fragment's frontmatter. The `--match` mode controls how the path is compared:

  ```bash
  # Exact path equality (the default).
  creek purge source --source-path ~/exports/unwanted.zip --vault ~/Obsidian/Creek-Vault

  # Substring containment — handy when staged files share a directory.
  creek purge source --source-path /exports/2026-04 --match substring --vault ~/Obsidian/Creek-Vault

  # Regex — fail fast on a malformed pattern; no silent mismatches.
  creek purge source --source-path "diary-2026-04-2[0-9]\.md$" --match regex --vault ~/Obsidian/Creek-Vault
  ```

Pass exactly one of the two modes; mixing them is a usage error. Both modes record the chosen criteria — including `--match` — to the audit log so an operator can reconstruct what each purge actually targeted.

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

Nuclear option: destroys every fragment, thread, eddy, and resonance. Leaves the directory structure and `00-Creek-Meta/` intact. This is **never** undoable.

Before the wipe loop runs, the engine verifies that the target directory is a Creek vault by checking for the `00-Creek-Meta/creek_config.yaml` marker file that `creek init` deploys (GAP-003). The check runs *before* the intent audit line is written, so a refusal leaves the audit log untouched. Both the interactive and `--force-non-interactive` paths go through the same check — no carve-out. A `--vault` typo that points at an unrelated directory with coincidentally numeric-prefix folders therefore exits non-zero with a clear message naming the marker the engine looked for, rather than silently wiping the wrong tree.

Outside of `--dry-run`, the command then refuses to proceed unless one of these is true:

- **Interactive run.** stdin is attached to a real TTY and the operator types the **absolute path** of the vault when prompted (not the literal string `"yes"`). Typing the wrong path aborts.
- **Explicit non-interactive opt-in.** The command was invoked with `--force-non-interactive` *and* a valid `--confirm-text "I understand this is irreversible"`. This path emits a `WARNING` log entry so the audit trail records the bypass.

A piped or redirected stdin without `--force-non-interactive` exits non-zero with a clear message — closing the OPS-002 gap where a misbehaving cron job could satisfy the prompt programmatically.

```bash
# Interactive (recommended): prompts for the absolute vault path.
creek purge vault --vault ~/Obsidian/Creek-Vault

# Explicit non-interactive opt-in (CI / scripted teardown):
creek purge vault \
    --vault ~/Obsidian/Creek-Vault \
    --confirm-text "I understand this is irreversible" \
    --force-non-interactive
```

#### Migration note (OPS-002)

Earlier versions accepted any of the following as valid confirmation:

```bash
# Old (≤ Batch G-1): both forms worked.
echo "I understand this is irreversible" | creek purge vault --vault ... --yes
creek purge vault --vault ... --confirm-text "I understand this is irreversible"
```

Both now fail closed:

- **Piping the phrase** is rejected because stdin is no longer a TTY. Wrap the call with `--force-non-interactive` and pass `--confirm-text` explicitly.
- **The interactive prompt** no longer accepts the literal phrase; it asks for the absolute vault path. Operator runbooks that scripted the old phrase need updating to type the path instead.

If your CI or teardown scripts depended on the old behaviour, the equivalent is:

```bash
creek purge vault \
    --vault "$VAULT" \
    --confirm-text "I understand this is irreversible" \
    --force-non-interactive
```

The `WARNING` log entry written when `--force-non-interactive` is used will surface in `<vault>/00-Creek-Meta/audit/` going forward, giving you a record of every bypass.

---

## Audit trail

Every purge writes **two** JSONL lines to `<vault>/00-Creek-Meta/audit/purge.jsonl`: an `intent` line *before* the first destructive op, then an `outcome` line *after* the body completes (or after an exception aborts it). Both lines share a UUID4 `operation_id` so a recovery tool can pair them, and both extend the `prev_hash` sha256 chain so a tampered or truncated log can be detected via the verification API:

```json
{
  "operation": "vault",
  "criteria": {"scope": "entire vault"},
  "operator": "sgsg",
  "timestamp": "2026-04-28T18:01:23Z",
  "phase": "intent",
  "operation_id": "9f1c…",
  "dry_run": false,
  "prev_hash": "0000…"
}
{
  "operation": "vault",
  "criteria": {"scope": "entire vault"},
  "affected_fragments": ["frag-…", "frag-…"],
  "operator": "sgsg",
  "timestamp": "2026-04-28T18:01:24Z",
  "phase": "outcome",
  "operation_id": "9f1c…",
  "status": "complete",
  "fragments_deleted": 47,
  "references_scrubbed": 312,
  "embeddings_removed": 47,
  "dry_run": false,
  "prev_hash": "abcd…"
}
```

If the process is killed *between* the two writes (SIGKILL, power loss, OOM kill), the intent line is on disk and the outcome line is not. An operator inspecting the log knows that vault `<id>` was being attempted at `<timestamp>` and can reconcile against the filesystem. If the body raises a Python exception, the engine writes an outcome line with `status="partial"` and a `failure_reason` field naming the exception type and message; the original exception then propagates.

`creek purge` is **not** transactional — there is no staging-directory rename pattern, so a crash partway through `_wipe_folder_contents` can still leave the filesystem half-deleted. The intent + outcome pair is the recovery contract: it tells you *what was attempted* and *how far it got*, but does not roll back. Pre-GAP-002 entries (no `phase` field) read back as `phase="outcome"` with `operation_id=""` and `status=null` for backward compatibility.

The outcome line's `embeddings_removed` is the real number of rows dropped from
`<vault>/00-Creek-Meta/embeddings.parquet` in that call (GAP-001):

- Zero when the embeddings cache has not been built yet (no `creek
  link` run has materialised the parquet).
- The exact row delta when the cache exists — *not* mirrored from
  `fragments_deleted`. Purging a fragment that was never embedded
  reports `embeddings_removed: 0` even though `fragments_deleted: 1`.
- For `creek purge vault`, this counts every row that was in the
  cache file the engine just deleted outright.

`creek redact --apply` writes alongside it at `<vault>/00-Creek-Meta/audit/redact.jsonl`. Privacy-tier overrides (e.g. `creek mine --include-tier intimate`) write to `<vault>/00-Creek-Meta/audit/privacy.jsonl`. Operational provenance from ingestion stays at `<vault>/00-Creek-Meta/Processing-Log/provenance.jsonl` (separate location: not compliance-grade, allowed to be lossy).

A pre-Batch-C `Processing-Log/purge-log.json` from older installs is migrated automatically on first read or write — every legacy entry is replayed into the new chain, a `purge.audit.migration` marker is recorded, and the old file is removed.

The audit trail itself is **not purgeable** by `creek` — it's the system's compliance record. You can `git rm` it manually, but that's outside the tool.

## Recovery

`creek purge` works at the filesystem level — fragments are deleted from `01-Fragments/`. If you ran the wrong purge:

- **If you have a clean git working tree**: `git status` will show the deletions; `git checkout -- 01-Fragments/` restores them. Re-run `creek link` to rebuild references.
- **If you've already committed**: `git revert` or `git restore --source HEAD~1`.
- **If you haven't been committing** (don't): the only recovery is to re-ingest from the original sources.

This is one of several reasons the top-level repo is git-tracked.
