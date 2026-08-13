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

**The ledger is the authority on a known unit's identity.** Once a
`source_key` has a record, every branch below reuses `record.fragment_id` —
including the *unchanged* branch, which until #1329 wrote whatever id the
ingestor had just derived. That distinction matters more than it sounds: it
means a change to *how* ids are derived can never silently duplicate a
fragment the ledger already knows about. Derivation is only how a **new** unit
gets its first id.

## One-time migration: `creek ingest --pin-source-ids`

**If you have a vault created before this release, run this once:**

```bash
creek ingest --pin-source-ids --vault <vault>          # add --dry-run to preview
```

### Why

`generate_fragment_id` hashes the fragment's timestamp, and for a file with no
embedded date that timestamp came from the filesystem. Two host-dependent
inputs leaked into it (#1329):

- `datetime.fromtimestamp(mtime)` with no `tz=` rendered the epoch in the
  *host's local zone*, so one file minted a different id in every timezone —
  move a laptop, or run CI in UTC while working in Los Angeles, and the next
  ingest saw a "new" fragment.
- `st_birthtime` exists on macOS/BSD and not on Linux, so the same file minted
  a different id on a developer's Mac than in Linux CI.

Both are fixed: markdown, documents and images now derive that fallback
through `creek.ingest.base.file_modified_time`, a pure function of the file's
epoch mtime in UTC. But fixing the derivation **changes** it, so without the
migration the next `creek ingest` over an existing vault would fail to
recognise its own fragments and write duplicates.

### What it does — and what it refuses to do

The migration **pins**; it does not re-mint. Existing ids never move.

- **Ledger** — one record per markdown-sourced fragment, carrying the id read
  off disk. Never a recomputed one. That is what makes the whole re-mapping
  problem (renaming files, rewriting the id index, children, resonance edges,
  the embedding cache, thread membership, provenance) *vacuous* rather than
  merely skipped: none of those references move, so none of them need
  rewriting.
- **Frontmatter** — exactly one key is added, `source.origin_key`. `id`,
  `created`, the body and the filename are byte-identical afterwards. The
  stamp is mandatory rather than cosmetic: the RTBF purge sweep resolves its
  target by reading that field off disk and skips fragments without it, and
  `update_fragment` preserves on-disk frontmatter rather than merging fresh
  `source` fields — so a fragment lacking the key at migration time would
  never gain one on any later ingest. Writes are atomic.
- **Already-duplicated vaults** — a `source_key` claimed by more than one live
  fragment is pinned for *neither*, and both paths are printed. Blessing one
  would orphan the other forever. Resolve with `creek clean duplicates`, then
  re-run.
- **Unresolvable sources** — a fragment whose source has been deleted, or that
  records no source file, is listed and skipped rather than guessed at.

It is idempotent (a second run pins nothing) and `--dry-run` writes nothing.

Run it from wherever you like. `source.original_file` is recorded verbatim, so
a vault ingested with a relative `--source` holds relative paths; a relative
record that names nothing in the current directory is resolved against the
vault root before it is called missing. Without that, running this one-shot
migration from a different directory than the original ingest reported live
sources as deleted and skipped exactly the fragments it exists to protect.

An un-migrated vault is detected on the next ingest — an empty **markdown**
ledger over a vault that already holds fragments is the marker — and the
advisory naming the remedy is printed **before that run's write pass begins**,
so an operator watching it can still abort with nothing yet duplicated. This
covers every ingest driven through the CLI, `creek sync` included.

The ledger it weighs is always the markdown one, never whichever ledger the
current run resolved. A run may borrow another ledger for identity
(`run_ingest(ledger_source=…)`, as the `creek.upload` MCP tool always does),
and judging that borrowed ledger's emptiness would both warn about vaults
already migrated and — the worse half — go permanently quiet about vaults that
are not, as soon as the borrowed ledger gained its first record. The advisory
is scoped by the run's `source_type` instead: `#1329` moved *markdown* id
derivation only, so a document or image run is never warned, and no
`ledger_source` override can change what a run's `source_type` is.

It is an advisory, not a gate: the run continues and the exit code is
unchanged. A warning describes vault state that will cause trouble, not a
failure of the run in front of it, and making it fatal would hard-fail every
scheduled `creek sync` over a vault that has not been migrated yet. So an
*unattended* ingest over an un-migrated vault will still duplicate it. Pin
first.

### Two behaviour changes worth knowing about

1. **On macOS, a markdown fragment's `created` now reports modification time,
   not birth time.** That is the price of an id that does not depend on which
   operating system ingested the file, and the trade is deliberate. Because
   the writer builds a fragment's filename from `created`, a **newly ingested**
   fragment's date prefix may differ from what the old code would have chosen.
   No existing file is renamed — the migration never touches `created`.
   Authorship remains `authored_at`'s job (FEAT-031), which has its own
   frontmatter fields and its own backfill (`creek ingest --refresh-dates`).
2. **Documents remain unledgered.** The derivation fix alone stops their
   *timezone*-driven duplication, but a document whose mtime moves still
   re-derives its id with no ledger record to pin it. Widening the ledger to
   documents is blocked on a real defect in `derive_source_key` and is tracked
   separately (#1363).

## What happens on ingest

For each source unit, the ledger drives an **unchanged / changed / gone**
decision:

| Case | Detection | Behaviour |
|------|-----------|-----------|
| **unchanged** | content hash matches the ledger | idempotent no-op (the write resolves to the existing fragment under the **ledgered** id) |
| **changed** | same `source_key`, new content hash | **update in place** — the existing fragment is rewritten under its preserved id, keeping classifications and resonance links |
| **gone** | a ledgered `source_key` is absent from a full-source pass | **soft-tomb** — the fragment moves to `10-Liminal/Orphaned/` and is marked `lifecycle: orphaned` (never hard-deleted) |
| **re-added** | a tombed `source_key` reappears | **restore** — the tombed fragment is moved back and un-marked under its preserved id |

### The ledger is destroyed by a purge (#1453)

The ledger has one more lifecycle event than the table above: **erasure**. It
is the vault's stored mapping from a source path to a fragment id to a full
unsalted SHA-256 of that source's content, so a right-to-be-forgotten request
has to take it with them.

- `creek purge vault` destroys every `00-Creek-Meta/State/ingest/*.jsonl`
  outright, as part of the deny-by-default sweep of `00-Creek-Meta/`.
- `creek purge fragment` / `source` / `source-path` / `daterange` erase the
  rows naming the purged ids, plus every row naming a source unit those ids
  came from — the ledger is append-only, so a superseded row still carries the
  source path and an earlier draft's content hash. A ledger file left with no
  rows is unlinked.
- `creek purge classifications` touches no rows. It deletes nothing, and
  wiping its rows would re-mint an id for every unchanged file in the vault on
  the next ingest.

**This changes the unchanged / changed / gone contract after an erasure**, and
the change is not subtle:

- Every remaining source unit is **created**, not **unchanged** — there is no
  record to compare a hash against. A re-ingest of an untouched source
  directory reports creations, not no-ops.
- Nothing is **tombed**. `live_keys()` is empty, so the gone branch has no
  ledgered key to miss and a full-source pass over an emptied vault reports
  `0 tombed`. That matters: without the erasure the gone branch would see
  every purged unit as vanished and soft-tomb fragments that no longer exist.
- The `_UNPINNED_VAULT_WARNING` advisory stays silent, because it is guarded on
  the vault holding fragments — after a vault purge it holds none.
- **The re-ingested fragment carries the same id as before the purge.** The
  ledger is not what reissues it: `generate_fragment_id` hashes source path,
  timestamp and content, and `MarkdownIngestor._resolve_timestamp` is a pure
  function of epoch mtime specifically so ids reproduce. Remove the ledger
  directory from a vault entirely and the same file still re-ingests under the
  same id. Anyone who can re-derive that id already holds the plaintext it was
  derived from; what the erasure removes is the vault's own record of the
  mapping. Making a purged id unreissuable would require salting fragment ids —
  a separate change touching dedup, the writer id-index, and every
  deterministic-id test.

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
- A material edit additionally **re-derives `privacy_tier`** (and the
  `voice_proxy_eligible` flag computed from it) from the *new* body (#922).
  The tier cannot simply be cleared and left for the next pass the way the
  classification keys are: while it is stale it still governs read-side
  admission, so a fragment rewritten into intimate content would stay visible
  to an `open`-ceiling reader in the meantime. The new tier comes from the
  deterministic, LLM-free privacy heuristic and is merged **escalate-only**
  against the tier already on disk — a rewrite can raise a tier but never
  lower one, so an operator's `intimate` survives a benign rewrite.

The threshold is the config knob
`classification.reclassify_on_edit_threshold` (default `0.9`, range `0.0–1.0`).
Set it to `0.0` to always preserve classifications across edits.

## Summary output

`creek ingest` prints a one-line summary of what changed:

```
Ingest summary: 3 created, 1 updated, 1 tombed
```

- **created** — source units with no prior ledger record (new fragments).
- **updated** — units whose content changed (in-place updates and restores).
  Unchanged re-ingests are idempotent no-ops and are **not** counted here, so
  the summary reflects only what actually changed.
- **tombed** — units soft-tombed this run because their source vanished.
