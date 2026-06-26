# Idempotent ingest of mutable sources

Creek keeps the vault current with **mutable** sources — the ones you edit
repeatedly, like the Obsidian journal — without minting duplicate fragments or
leaving orphans behind. This is epic #668 (SPEC R1).

## The model

Fragment ids are content-addressed (`frag-sha256(source:timestamp:content)`),
so *append-only* sources (Discord/ChatGPT/Claude/Substack exports) are naturally
idempotent: the same message always hashes to the same id. But a **mutable
document** changes content under a stable identity (the file), so a pure
content-hash id would mint a new fragment on every edit and orphan the old one.

To fix that, each mutable source unit carries a stable **`source_key`** and is
tracked in a per-source **ledger**:

- **`source.origin_key`** — frontmatter field holding the vault-relative path of
  the source unit (e.g. `personal/journal/2026-06-26.md`). This is the stable
  identity an edit is matched on, distinct from the content-hash id.
- **Ledger** — append-only JSONL at
  `00-Creek-Meta/State/ingest/<source>.jsonl` mapping
  `source_key → {fragment_id, content_hash, last_seen, tombed}`. The latest line
  per key wins on load; malformed/partial rows are skipped.

Today the markdown (journal) source is ledger-wired; append-only event sources
keep their content-hash ids untouched.

## What happens on ingest

For each source unit, the ledger drives an **unchanged / changed / gone**
decision:

| Case | Detection | Behaviour |
|------|-----------|-----------|
| **unchanged** | content hash matches the ledger | idempotent no-op (deterministic id dedups the write) |
| **changed** | same `source_key`, new content hash | **update in place** — the existing fragment is rewritten under its preserved id, keeping classifications and resonance links |
| **gone** | a ledgered `source_key` is absent from a full-source pass | **soft-tomb** — the fragment moves to `10-Liminal/Orphaned/` and is marked `lifecycle: orphaned` (never hard-deleted) |
| **re-added** | a tombed `source_key` reappears | **restore** — the tombed fragment is moved back and un-marked under its preserved id |

### Full-source vs single-file

The **gone** branch only runs on a *full-source directory* pass (the whole
journal folder is scanned). A single-file `creek ingest --input <file>` run never
tombs anything — it only does the unchanged/changed branch for that one unit.
This guards against a targeted re-ingest accidentally tombing every other unit.

## Re-classify on material edits

When an in-place update happens, Creek decides whether the edit is large enough
to warrant re-classification:

- The new body is compared to the prior body (a `difflib` similarity ratio).
- **Trivial** edit (ratio ≥ threshold — a typo, whitespace) → classifications
  and tags are **preserved**.
- **Material** edit (ratio < threshold — a real rewrite) → the fragment's
  `classification_method` / `classified_at` / `classification_reasoning` are
  cleared so the next `creek classify` pass re-does **only that fragment**
  (cooperating with OPS-001 — no global `--force`).

The threshold is the config knob
`classification.reclassify_on_edit_threshold` (default `0.9`, range `0.0–1.0`).
Set it to `0.0` to always preserve classifications across edits.

## Summary output

`creek ingest` prints a one-line summary of what changed:

```
Ingest summary: 3 created, 1 updated, 1 tombed
```

- **created** — source units with no prior ledger record (new fragments).
- **updated** — units that already had a ledger record (in-place updates,
  restores, and unchanged no-ops).
- **tombed** — units soft-tombed this run because their source vanished.
