# Cleaning and purge

Two adjacent command groups handle vault hygiene:

- **`creek clean`** — non-destructive maintenance: orphans, stale reviews, broken links, duplicates.
- **`creek purge`** — destructive deletion (right-to-be-forgotten): fragments, sources, classifications, date ranges, the whole vault.

`clean` finds problems and reports them. `purge` deletes things. They're separate tools because mistaking one for the other would be expensive.

`clean` has **no write mode at all** — no `--apply`, no `--fix`. Its scanners expose `scan()` and nothing else. Until #1039 the four scan subcommands each carried an `--apply` flag that printed a red `APPLY` banner and then changed nothing; the flag was removed rather than implemented. Acting on a `clean` report means running `creek purge`, by hand, on the entries you chose.

> Purge does best-effort deletion. It is **not** anti-forensic — modern SSDs and copy-on-write filesystems retain old block contents even after a successful unlink. See the [threat model](security/threat-model.md) for the full set of guarantees Creek does and doesn't make.

---

## Cleaning

### `creek clean orphans`

Identifies fragments with **zero incoming and outgoing links** that are older than N days. These are typically failed ingestions, single-shot notes that never connected to anything, or fragments whose links pointed to since-deleted siblings.

```bash
creek clean orphans --vault ~/Obsidian/Creek-Vault --age-days 30
```

Links are resolved by every name a page answers to — filename stem, frontmatter `title`, and each `aliases` entry — not by filename alone (#1225, the same fix #887 made for `broken-links`). A fragment linked as `[[Messages]]` credits the page whose file is `2020-09-26-Messages.md`, so it is not reported. A link that only points at its own file does not rescue the page from the list.

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
3. **Scrubs every reference** — wiki-links pointing at the deleted fragment(s) are removed from every other `.md` file in the vault (matched by title), and every word-boundary mention of the deleted fragment ID is replaced with the `[purged]` placeholder across YAML frontmatter (e.g. `source_fragments: […]` in drafts and mining ideas) and body text. The walk covers all `.md` files in the vault, including `04-Praxis/`, `05-Wavelength/`, `06-Frequencies/`, `07-Voice/Drafts/`, `08-Decisions/`, `09-Reference/`, `10-Liminal/`, and `00-Creek-Meta/Skills/` (deployed skill tree) — treat this list as illustrative, not exhaustive, when doing a post-purge audit. The compliance audit log itself (`00-Creek-Meta/audit/purge.jsonl`) is JSONL, not Markdown, and is intentionally excluded — `affected_fragments` in audit entries keeps the real ID for forensic reconstruction (GAP-004).
4. **Removes embeddings** from `<vault>/00-Creek-Meta/embeddings.parquet` (GAP-001). Per-fragment / per-source / per-date-range purges drop matching rows; `creek purge vault` deletes the file outright.
5. **Sweeps the intimate-body stub** (GAP-012). `creek save` of an `intimate`-tier answer writes a title-only note and routes the full body to a stub under `10-Liminal/Compost/intimate-stubs/`, recording the link in the note's `saved_from.intimate_body_pointer`. Whenever a scoped purge (`fragment` / `source` / `daterange`) deletes a note carrying that pointer, it now follows the pointer and deletes the stub too, so the full intimate body does not survive the request. The pointer is resolved relative to the vault root; a missing/empty pointer, an already-deleted stub, or a pointer that resolves outside the vault are all tolerated as no-ops. The count of stubs removed is reported in the audit `outcome` entry as `intimate_stubs_removed`. (`creek purge vault` already removes the stub by wiping all of `10-Liminal/`.)
6. **Sweeps the staged Adepthood source** (#845, widened by #1023). The two Adepthood MCP write tools keep the raw material they were handed inside the vault: `creek.journal` stages each entry's full body as markdown under `00-Creek-Meta/adepthood/journal/`, and `creek.upload` stages each uploaded document's bytes verbatim under `00-Creek-Meta/adepthood/uploads/`. Both record that vault-relative path in the produced fragment's `source.origin_key`, and every scoped purge (`fragment` / `source` / `daterange`) follows that key and deletes the staged file too — otherwise the full plaintext entry, or the whole uploaded document, outlives the request that was supposed to erase it. **Both roots are swept, not just the journal one**: an upload that never reached the sweep would recreate #845 on a new surface, silently, with a green journal-only test suite. Deletion is scoped to the staging roots themselves, so a hand-edited or malicious `origin_key` (`../x`, `01-Fragments/other.md`) is logged and ignored rather than followed; a missing, empty, or already-deleted key is a no-op. `creek purge vault` sweeps both directories wholesale, because it deliberately preserves `00-Creek-Meta/` and the content-folder wipe would otherwise never reach them; that walk is non-recursive and skips anything that is not a file, so a stray subdirectory cannot abort a whole-vault RTBF request. The count is reported in the audit `outcome` entry as `journal_staged_removed` — a journal-era name kept deliberately, because `purge.jsonl` is append-only and a rename would break every log already written.
7. **Sweeps the derived voice artifacts** (#1211). The voice subsystem does not merely reference a fragment, it **copies its body**, so step 3's reference scrub leaves the erased content on disk in three shapes: `07-Voice/Register-Samples/<register>/<id>.md` is a byte-for-byte `copy2` of the fragment file (#879); `07-Voice/<register>-profile.md` renders each exemplar body verbatim under `### Sample Passages`; and `07-Voice/Lexicon/glossary.md` plus `07-Voice/Lexicon/Metaphors/<domain>.md` quote whole source sentences. Every scoped purge (`fragment` / `source` / `daterange`) deletes the matching artifacts, and it does so **before** the reference scrub — the lexicon's only link back to the fragment is a `[[<id>]]` wikilink, which the scrub rewrites to `[[[purged]]]`, so a sweep running afterwards would be blind to exactly the notes quoting the body. Matches are **deleted, not edited**: a derived note is a function of the corpus, so excising one passage would leave the purged fragment's statistical residue (its n-grams, its contribution to every count) behind while the note went on advertising a total it no longer has. All four are regenerated by `creek report --type voice` and `creek report --type lexicon`. The count is reported in the audit `outcome` entry — and by the CLI — as `voice_artifacts_removed`.

   **Re-run the reports afterwards.** These are *shared* derived notes: the profile or glossary deleted because it quoted the purged fragment also held every other fragment's legitimately-retained content, and that content stays gone until `creek report --type voice` and `creek report --type lexicon` regenerate it from the corpus that remains. The CLI prints the same reminder whenever `voice_artifacts_removed` is non-zero.

   Three boundaries are deliberate. `07-Voice/Rhetorical-Patterns/<register>.md` is **not** swept: it holds move *counts* only, so nothing attributable to a fragment lands there and deleting it would be destruction without a leak to justify it. `07-Voice/Drafts/` is **not** swept either: those are the operator's own essays, handled by the provenance scrub, and a fragment purge has no mandate to delete them. `07-Voice/Register-Samples/_Summary.md` is likewise left alone — it is #879's manifest, which the next prune needs in order to remove stale copies, and it normally carries counts rather than body text. That last claim is *not* unconditional: `_is_safe_sample_stem` in `creek/generate/voice.py` documents a crash window (#879 territory) in which a fragment body can land in `_Summary.md` instead. Closing that race is out of scope for this sweep.

   One design gap is recorded rather than papered over: a register profile stores exemplar **bodies and no fragment ids at all**, so there is no recorded provenance to key its sweep on. That pass therefore matches on the purged fragment's own body text — exact, since the generator emits each passage verbatim, and still reached from the id the caller holds (id → fragment file → body) — but it is a content match standing in for a link the format never wrote down. Recording exemplar ids in profile frontmatter would make it a real one.

   That content match has one failure mode, and it is reported rather than swallowed. The body is re-read from the fragment file under **strict** UTF-8, never taken from the lossy read the purge's match loader performs for a non-UTF-8 file (#910) — a body with U+FFFD substituted for its bad bytes cannot match a profile quoting the real ones, so the sweep would find nothing and still report success. When the strict read fails, the id-keyed passes still run (the `Register-Samples` stem and the `[[<id>]]` lexicon wikilink do not depend on the body, so those artifacts are still erased), a `WARNING` names the fragment id, the id appears on the result's `voice_body_undecodable`, the CLI prints `Voice sweep INCOMPLETE for: <id>`, the MCP payload reports `status: "partial"` and carries the `voice_body_undecodable` list (#1246, contract `0.4`), and the audit `outcome` line is written with `status="partial"` and `failure_reason="UnicodeDecodeError"`. An erasure that fell short never certifies itself as complete — on any surface. Whether a result is complete or partial is decided in one place, `PurgeResult.outcome_status`, which is what keeps the audit line and the MCP payload from disagreeing about the same purge.

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

The engine also reads every fragment under `01-Fragments/` for its `id` before the wipe runs (#1340) — the census has to happen while the files still exist, because it is what lets the audit log name what the wipe is about to destroy. That read is the operation's one scaling cost: `creek purge vault` now scales with fragment count rather than folder count, the same O(fragments) cost `creek purge fragment` / `source` / `daterange` already pay for parsing every fragment in `01-Fragments/`. Measured at roughly 1.1s per 5,000 fragments, so on the order of 8s for a 35k-fragment vault — a real cost against a wipe that used to be O(folders), but small beside the `rmtree` it precedes.

Before this fix, the audit log's fragment count was the *folder* count: `fragments_deleted` reported `len(deleted_files)`, and `deleted_files` held only the top-level entries of each content folder. A vault holding 500 fragments in `01-Fragments/Conversations` wrote `fragments_deleted: 3` and `affected_fragments: []` to `purge.jsonl` — three directory entries, two of them empty — while destroying all 500; it now writes `fragments_deleted: 500` and all 500 ids. `deleted_files` — the list the CLI renders as its `Deleted files` table, capped at 20 rows with a note of how many it hid — now names the regular files a wipe destroyed, recursively, and never the directory that held them; an empty directory contributes no entry and is still removed from disk. A symlink is never walked through: a symlink to a file is named, because the alias really is unlinked, but a symlink to a directory contributes nothing, because `shutil.rmtree` refuses to walk into one and the tree behind it is not this purge's to claim.

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

`fragments_deleted` and `affected_fragments` are deliberately asymmetric — for every purge operation, not just `vault`. `fragments_deleted` counts **every** `.md` file the wipe touches, including one with no `id` and one whose YAML will not parse: the wipe destroys those too, and an erasure record that under-counts destruction is the worse error. `affected_fragments` lists only the subset that declared a string `id`, because naming an id nobody recorded would be a fabrication in a compliance record. On a vault with hand-edited or malformed fragments the roster is therefore a genuine *subset* of the count — `len(affected_fragments) < fragments_deleted` is expected, not corruption.

If the process is killed *between* the two writes (SIGKILL, power loss, OOM kill), the intent line is on disk and the outcome line is not. An operator inspecting the log knows that vault `<id>` was being attempted at `<timestamp>` and can reconcile against the filesystem. If the body raises a Python exception, the engine writes an outcome line with `status="partial"` and a `failure_reason` field naming only the exception type (e.g. `OSError`), never its message — the audit trail is not purgeable (see below), so a message quoting vault-derived content would outlive the very right-to-be-forgotten request that produced it; the original exception, with the full message, still propagates to the caller.

`creek purge` is **not** transactional — there is no staging-directory rename pattern, so a crash partway through `_wipe_folder_contents` can still leave the filesystem half-deleted. The intent + outcome pair is the recovery contract: it tells you *what was attempted* and *how far it got*, but does not roll back. Pre-GAP-002 entries (no `phase` field) read back as `phase="outcome"` with `operation_id=""` and `status=null` for backward compatibility.

For `creek purge vault`, that recovery contract extends to the fragment count itself. The census of what the wipe is about to destroy has to run *before* it — the files are about to stop existing — but its numbers are committed to the result only *after* the destructive section completes. If the wipe raises partway through, the `status="partial"` outcome line reports `fragments_deleted: 0` and an empty `affected_fragments`, rather than certifying an erasure of files that, at that point, were still on disk.

The outcome line's `embeddings_removed` is the real number of rows dropped from
`<vault>/00-Creek-Meta/embeddings.parquet` in that call (GAP-001):

- Zero when the embeddings cache has not been built yet (no `creek
  link` run has materialised the parquet).
- The exact row delta when the cache exists — *not* mirrored from
  `fragments_deleted`. Purging a fragment that was never embedded
  reports `embeddings_removed: 0` even though `fragments_deleted: 1`.
- For `creek purge vault`, this counts every row that was in the
  cache file the engine just deleted outright.

The outcome line also carries `intimate_stubs_removed` (GAP-012): the
number of intimate-body stub files under
`10-Liminal/Compost/intimate-stubs/` that the engine deleted because a
purged note pointed at them via `saved_from.intimate_body_pointer`. The
sweep is scoped to that directory: a pointer resolving anywhere else in
the vault is refused and its target left untouched, so the pointer can
never be used to steer a delete at an arbitrary vault file. Zero for
notes that carry no pointer, and the counter is not incremented for a
refused pointer either — in a dry run or a real one. A dry-run
otherwise reports what *would* be removed without touching disk.

It also carries `voice_artifacts_removed` (#1211): the number of
derived `07-Voice/` notes deleted because they held the purged
fragment's own content — its `Register-Samples` file copy, the
`<register>-profile.md` quoting its body, and the `Lexicon` notes
quoting its sentences. Zero when no voice report has ever run. The
sweep is scoped to those declared locations and every candidate is
resolved and re-checked for containment inside `07-Voice/` before the
counter moves, so a register folder symlinked out of the vault is
logged and refused rather than followed — and a dry run never previews
a deletion the real run would decline to make. When a purged fragment's
body cannot be decoded as strict UTF-8, the content-keyed profile pass
is skipped for it and the entry is written with `status="partial"` and
`failure_reason="UnicodeDecodeError"` — the audit trail records an
incomplete erasure as incomplete.

A dry run's counters now agree with what the same call would apply, exactly, across `intimate_stubs_removed`, `provenance_scrubbed`, `voice_artifacts_removed`, and `wikilinks_removed` (#1340). They used to diverge for two independent reasons: a counted-only deletion left the artifact on disk for a later pass in the same run to find and count again, and a counted-only rewrite left the *old* bytes on disk so a later pass matched references the real run had already scrubbed. Both are closed by a per-operation dry-run ledger that records what an apply run would have deleted and written; the engine populates it on every run but consults it only under `dry_run`, so nothing the ledger holds can change what a real purge deletes or rewrites.

`creek redact --apply` writes alongside it at `<vault>/00-Creek-Meta/audit/redact.jsonl`, using its own three-phase schema — `phase` (`intent` / `file` / `outcome`), `operation_id`, `status`, `failure_reason` and `files`, on top of the per-file `source_path` / `pattern_names` / `match_counts` fields. It is documented in full, including what an `intent` line without an `outcome` line does and does not prove, at [redaction.md → The audit trail](redaction.md#the-audit-trail); the table above describes `purge.jsonl` only. Note that `creek redact --apply` never rewrites `00-Creek-Meta/audit/` or the legacy `Processing-Log/purge-log.json`, so redaction cannot launder records into the purge chain via the legacy-migration path. Privacy-tier overrides (e.g. `creek mine --include-tier intimate`) write to `<vault>/00-Creek-Meta/audit/privacy.jsonl`. Operational provenance from ingestion stays at `<vault>/00-Creek-Meta/Processing-Log/provenance.jsonl` (separate location: not compliance-grade, allowed to be lossy).

A pre-Batch-C `Processing-Log/purge-log.json` from older installs is migrated automatically on first read or write — every legacy entry is replayed into the new chain, a `purge.audit.migration` marker is recorded, and the old file is removed.

The audit trail itself is **not purgeable** by `creek` — it's the system's compliance record. You can `git rm` it manually, but that's outside the tool.

That retention point sharpens now that `creek purge vault` names every fragment it destroys (#1340): the roster written to `purge.jsonl` after a whole-vault purge is the complete list of ids that vault held under `01-Fragments/`. A fragment id is a truncated SHA-256 over source, timestamp, and content — not reversible, and it confirms membership only to someone who already holds the exact original — but it is a durable record of *what existed*, and it outlives the erasure that produced it. An operator who needs that roster gone has to remove the log out of band, by the same `git rm` above. At 35k fragments the roster is roughly 700 KB on a single JSONL line; the hash chain is unaffected, because verification hashes the stored line bytes, not anything derived from them.

The roster is not the only thing that survives, and it is not the most identifying. `purge_vault` preserves `00-Creek-Meta/` apart from the Adepthood staging roots and the embeddings cache, so the ingest ledger under `00-Creek-Meta/State/ingest/` also outlives a whole-vault purge — and it maps each fragment id to the **source path** that produced it, which the roster does not. Whether that is intended is tracked in #1453; until it is settled, treat "what survives a vault purge under `00-Creek-Meta/`" as wider than the audit log alone.

## Recovery

`creek purge` works at the filesystem level — fragments are deleted from `01-Fragments/`. If you ran the wrong purge:

- **If you have a clean git working tree**: `git status` will show the deletions; `git checkout -- 01-Fragments/` restores them. Re-run `creek link` to rebuild references.
- **If you've already committed**: `git revert` or `git restore --source HEAD~1`.
- **If you haven't been committing** (don't): the only recovery is to re-ingest from the original sources.

This is one of several reasons the top-level repo is git-tracked.
