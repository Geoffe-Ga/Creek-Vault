# Changelog

All notable changes to `creek-tools` are documented here. Versioning is
loose during pre-1.0 development; the headings below track design-trace
IDs (`FEAT-*`, `INC-*`, `BUG-*`, …) embedded in commit messages and
inline code annotations. The original `plans/git-issues/` directory of
long-form spec files was retired in #243; use `git log --grep='<ID>'`
to locate the originating commit for any reference below.

## Unreleased

### Added

- **`creek.redact.scan` publishes the escaping-symlink skip count (#1292).**
  The tool's `statistics` object gains a fifth typed key,
  `files_skipped_symlink`: the number of symlinked children the scan declined
  unopened because their target resolves outside the scanned root. #1087 added
  that counter to `ScanSummary` and taught two of the three surfaces that
  render a scan about it — `generate_markdown_summary` and the `creek redact`
  stats table — and missed the third. A consumer was therefore told how many
  files were skipped as binary and by extension and never how many were
  declined for pointing out of the subtree, which is the one skip reason that
  means something tried to aim the scan somewhere it was not pointed.

  **The wire key is unconditional; the markdown row stays conditional on being
  non-zero.** That asymmetry is deliberate: a report line that fires on every
  scan is noise for a human reader, while a typed field whose presence varies
  is a second contract for a machine one.

  A tool's return shape moved, so the contract minor moves with it: **`0.11.0`
  → `0.12.0`**. No `/v1` route, capability, wire model, error code or status
  moves, so `SUPPORTED_CONTRACT_MINORS` **widens** rather than shifting and a
  `0.11` client keeps being served byte-identically on every route it calls.
  Nothing new is disclosed — the counter is a bare integer naming no path, no
  filename and no target; the identical count already reached the identical
  audience through `report_markdown`; and a refusal still carries the canonical
  four keys with no `statistics` block at all.

  The same change adds the guard #1087 lacked: `tests/test_mcp_redact.py`
  now holds a *census* mapping every non-`matches` field of `ScanSummary` to
  `published` or `withheld`, asserted total over the dataclass, so a future
  counter cannot reach — or fail to reach — a consumer without somebody
  recording which it is. `tests/test_adepthood_contract_models.py` gains
  `test_every_minor_below_the_current_one_is_still_served`, which derives the
  whole served range from the running minor instead of restating it by hand;
  the two hand-typed guards beside it had between them stopped covering `0.7`
  four bumps ago.

### Fixed

- **`redact --apply` no longer rewrites the vault it walks when `--vault` is
  omitted (#1561).** `run_apply` anchored its never-rewrite set on
  `config.vault_path`, which defaults to `Path()` — the process CWD — while
  `_exclude_audit_artifacts` was never told about `source`, the tree actually
  being walked. Run from a neighbouring directory, `--apply --source <vault>`
  rewrote 3 files instead of 1, including the vault's own
  `00-Creek-Meta/creek_config.yaml` and `Processing-Log/purge-log.json`:
  redaction silently reconfiguring redaction, and mutating legacy records that
  get chain-signed into `purge.jsonl` on next use.

  A second half: `_ApplyAudit` took the same defaulted root, so a destructive
  run against `<vault>` wrote its compliance record into a fabricated
  `<CWD>/00-Creek-Meta/audit/redact.jsonl` while the vault's own trail stayed
  empty. The successor to #1398, #1306, #1337 and #1338.

  The protected set and the audit log now derive from every vault root
  reachable from `--source`. Detection is unchanged: `--scan` and `--review`
  still report matches inside `00-Creek-Meta/`.

- **Untitled intimate saves no longer collide onto one stub file (#1509).**
  `_stub_relpath_for` read the raw `title`, which is `None` for an untitled
  save, so every untitled intimate save in a vault's lifetime targeted the
  single stem `intimate` and climbed `_atomic_create`'s counter ladder —
  `intimate-1.md`, `intimate-2.md`, … — raising at `_MAX_COLLISION_RETRIES`
  on the 1001st save and **losing that save entirely**, because the stub is
  written before the vault note. An untitled save is now addressed by a
  base32 digest of its own body: `intimate-<digest>.md`. The gate is on the
  **slug**, not the raw title, so an all-punctuation title like `"!!!"` —
  non-empty but slugifying to `""` — is body-addressed too rather than
  falling back to the bare stem.

  **On-disk naming change.** Existing stub files are never renamed and
  nothing migrates, so `intimate_body_pointer` values already written into
  vault notes keep resolving and purge's stub sweep still deletes them. Two
  byte-identical untitled bodies still collide and still use the counter, so
  the retry guard stays reachable.

### Tooling

- **`./scripts/check-all.sh` now matches CI gate-for-gate (GAP-007).**
  Three drifts closed:
  1. **Interrogate** (docstring coverage ≥95%) is now invoked between
     the tryceratops and unit-tests gates via the new
     `scripts/lint-interrogate.sh`. CI already runs it; previously a
     local pass could fail CI on docstring coverage alone.
  2. **Bandit severity** in `scripts/security.sh` switched from
     "all severities" to `-ll` (medium-or-above), matching CI and the
     CLAUDE.md §6.1 policy. The local gate is now no stricter than CI;
     low-severity findings are intentionally not gated. To audit them
     anyway, run `bandit -r creek/` directly.
  3. **`state-budget.sh` removed from `check-all.sh`.** The script
     silently no-ops when `CREEK_VAULT` is unset (its default state
     for most developers), so it had been a fake-pass entry on the
     local gate while CI never invoked it at all. It now ships as a
     standalone opt-in audit; run `./scripts/state-budget.sh --vault
     <path>` against a real vault when you want to check report-size
     budgets.

### Added

- **A per-sheet fragment now records which sheet it came from (#1392).**
  `sheet`, `rows` and `columns` reach the vault file via the writer's existing
  `extra_frontmatter` seam, gated by the explicit
  `PASSTHROUGH_FRONTMATTER_KEYS` allowlist in `creek/ingest/base.py`.
  Post-#1305 a workbook becomes one fragment per sheet, which made the dropped
  sheet name the only structured record of a fragment's origin. Implemented
  without `extra="allow"` (which would let any ingestor typo become permanent
  frontmatter) and without a nullable model field (which `_write_model`'s
  `model_dump(mode="json")` would print as `sheet: null` on every fragment in
  the vault). A fragment whose ingestor emitted no dimensions gains no keys.

### Breaking changes

- **The ingest ledger now hashes the body the vault actually holds, not
  `ParsedFragment.content` (#1393).** `SpreadsheetIngestor.parse` and
  `PresentationIngestor.parse` return `ParsedFragment(content="")` and build
  the real body in `convert_to_markdown`, so every workbook and every deck
  recorded `sha256("")` forever. An edited re-upload compared equal to its own
  predecessor, took the `unchanged` branch, and **the operator's edit was
  silently never written** while the run reported success. All three ledger
  hash sites — the recorded row, the changed/unchanged compare, and the
  `--since`/`--incremental` filter — now route through one `ledger_body_hash()`
  chokepoint, because fixing a subset is worse than fixing none: a ledger
  recording a rendered hash while the filter still hashed `""` would mark
  every workbook permanently changed and rewrite the corpus on every run.

  **No fragment id moves.** Only the argument to `ledger.content_hash`
  changed; every `generate_fragment_id` site is untouched. **Expect exactly
  one run to report `updated` instead of `unchanged`** for each affected
  source unit, as the ledger reconciles to the new convention. Markdown,
  documents and plain-text generic files are byte-identical under both rules
  (their converters are the identity function), so the `--pin-source-ids`
  /#1329 population sees no churn at all. For fenced-generic and image units
  the rewrite is byte-identical content, so classifications and privacy tiers
  are preserved. For spreadsheets and presentations, that one run is where
  previously-lost edits finally land.

- **`creek ingest` now reports `unchanged` in its summary (#1482).** The line
  printed created/updated/tombed/skipped, so a run that wrote fragments could
  print four zeros directly above `Ingested N fragment(s).` — most visibly
  after a `creek purge vault`, where the ledger row still matches but the file
  is gone and the "unchanged" branch recreates it. The counting was always
  correct; only the display omitted it. Anything parsing that line by position
  must account for the new field, which sits after `updated` rather than at
  the end so the invariant `written == created + updated + unchanged` reads
  left to right.

- **An `unclassified` `creek save` no longer writes its body into the vault in
  the clear (#1508).** `creek.classify.privacy_filter.pre_save_filter` branched
  on `tier == PrivacyTier.INTIMATE` and `tier == PrivacyTier.PERSONAL`, so
  `unclassified` — the tier every fragment carries until `creek classify` runs —
  matched neither name and fell through to the verbatim return. Meanwhile the
  MCP ceiling already treated that same tier as needing a `personal` ceiling to
  be *read* (#961), so the two halves of the privacy system disagreed about one
  tier: content the reader refused to serve, the writer filed in cleartext. Both
  thresholds are now read off `_TIER_RANK` through `tier_sensitivity`, so
  `unclassified` is summarised exactly as `personal` is, and a tier the ranking
  has never heard of is handled as `intimate` — summary in the vault, body to
  the gitignored `10-Liminal/Compost/intimate-stubs/` directory. `--full-body`
  is honoured at `unclassified` exactly as at `personal`, because the read side
  already normalises `UNCLASSIFIED` to `PERSONAL` before applying the same
  opt-in, and a save stricter than the read it feeds would contradict the
  ranking. **User-visible on both transports:** `creek save --tier unclassified`
  and a `creek.save` MCP call at that tier now file a `[Tier-redacted summary:
  …]` note instead of the body. Pass `--full-body` to keep the old behaviour.
  Nothing migrates notes already written in the clear. Alongside it,
  `creek_mcp.tier_ceiling.tier_allowed` stopped ending on a bare
  `_CEILING_RANK[ceiling]` subscript, which raised `KeyError` across the MCP
  boundary its own docstring promised not to raise across; an unrecognised
  ceiling is now refused rather than raised on. That branch is unreachable from
  production — `creek_mcp.policy._parse_ceiling` rejects an unknown ceiling
  first — so it is defence in depth, not a live hole.

- **An untitled `creek save` above `open` no longer takes its title from the
  body's first line (#1505).** `creek/save/writer.py` fell back to
  `_derive_title(request.body)` whenever `--title` was absent, at *every*
  tier. A title is not a redacted surface: it is written into the note's
  frontmatter in the clear, slugified into the **filename**, and — for
  `--target ai-as-user` — built into the fragment `id`, the note's stable
  handle, which other notes and Dataview queries quote back. So an untitled
  `--tier intimate` save published line 1 of the intimate body in a
  directory listing, the Obsidian sidebar, `git status`, and the
  `created_path` field of the hash-chained MCP audit log — none of which
  require opening the note — while the body it sat next to was correctly
  reduced to `[Tier-redacted summary: (untitled)]`. Untitled saves
  at any tier that `creek.classify.privacy_filter.tier_sensitivity` ranks
  above `open` — `unclassified` (which ranks with `personal`, #876),
  `personal`, `intimate` — are now titled `untitled <target> <8-hex content
  digest>` instead. `--full-body` does **not** relax the guard: it widens the
  body, which one reader opens on purpose, while the filename has the wider
  audience. `open` is untouched, and an operator-supplied `--title` is still
  written verbatim at every tier — only the operator can say whether their
  own title is safe. **User-visible:** untitled non-open saves get different
  filenames than they used to. Nothing migrates existing notes, so a live
  vault will hold both conventions; Obsidian links resolve by path, so no
  existing link breaks. The separate defect that an `unclassified` save
  writes its *body* in the clear is [#1508](https://github.com/Geoffe-Ga/Creek-Vault/issues/1508),
  which is fixed in its own entry above rather than here — the title half
  and the body half were kept apart on purpose.

- **`creek save --target paradox` now honours `--tier` instead of forcing
  `open` (#1491).** `creek/save/writer.py` used to substitute
  `PrivacyTier.OPEN` for any paradox save, so `--target paradox --tier
  intimate` filed the note with `privacy_tier: open` **and the full body in
  the clear**, while the CLI printed a yellow "will be widened" note and the
  `creek.save` MCP tool printed nothing at all. The MCP transport compounded
  it: the tool's response and its entry in the hash-chained audit log at
  `00-Creek-Meta/audit/mcp.jsonl` both reported `created_tier: intimate`
  while the artifact on disk was `open` — an auditor reading the
  tamper-evident log would have believed the note was protected. The read
  side inherited the same defect, because the stamped `open` defeats
  `_admitted_liminal_notes`' fail-closed check and served intimate-derived
  paradox bodies to open-ceiling consumers.

  Routing is unchanged — a paradox save still always lands in
  `10-Liminal/Paradoxes/`, and the *fact* of the contradiction is still
  preserved by the note's location, title, tags and `saved_from` provenance.
  Only the body moves: at `intimate` it is diverted to the gitignored
  `10-Liminal/Compost/intimate-stubs/` directory with
  `saved_from.intimate_body_pointer` naming it and the vault note reduced to
  a tier-redacted summary; at `personal` it is summarised unless
  `--full-body` is passed. **This is MCP-client-visible with no version
  negotiation** — a `creek.save` call at `target=paradox, tier=intimate` no
  longer returns a path to a note containing the body — mirroring the #1495
  precedent on the same surface. Paradox notes saved at `intimate` also now
  disappear from the Liminal Watch below `--include-tier intimate`. The
  widening warning is gone with the widening, and the deployed
  `paradox.SKILL.md` / `privacy-tier.SKILL.md` rules that told agents to route
  intimate material away from paradox have been rewritten.

- **`tier` is now required on every `creek.journal` and `creek.upload`
  call (#1494).** Both verbs previously declared `tier: str = "open"` —
  twice each, once on the tool function and again, independently, on the
  `build_server` wrapper that MCP clients actually reach — so a caller
  that omitted `tier` had its content filed as `open` and said so
  nowhere. That inverts the fail-closed rule the deployed
  `privacy-tier.SKILL.md` states as rule 1, "Never write `tier: open` by
  default": ordinary journaling is `personal` by that skill's own table,
  escalating to `intimate` for recovery, trauma or sexuality content, so
  the default was silently down-tiering exactly the material the tier
  system exists to protect. The `privacy_tier_ceiling` machinery could
  not catch it, because at `ceiling=open` a defaulted `open` is trivially
  within the caller's own ceiling. Both tools now return
  `{"status": "refused", ...}` naming the missing `tier`, before anything
  is staged, ingested, or audited. To preserve the old behaviour, pass
  `tier: "open"` explicitly. `PUT /v1/journal-entries/{external_id}` is
  unaffected — `JournalUpsertRequest.tier` never had a default — and
  `creek.upload` has no `/v1` route at all, so this restores MCP parity
  with the HTTP adapter rather than changing it. Vaults already
  scaffolded keep the old rule-5 `privacy-tier.SKILL.md` text, which
  names only `creek save`/`creek.save`, until `creek skills sync` or a
  re-`creek init` re-deploys the updated template.
- **`--tier`/`tier` is now required on every `creek save` and `creek.save`
  call (#1434); neither transport infers, defaults, or derives a tier
  from anything, including `--provenance`/`provenance` or the source
  fragments. This breaks any `creek save` caller that relied on
  `--provenance` implying `--tier open`, and any MCP client that called
  `creek.save` without a `tier`. You will find out immediately: the CLI
  exits 2 with a message naming `--tier`, and the MCP tool returns
  `{"status": "refused", ...}` naming the missing `tier`. To preserve
  the old behaviour, pass `--tier open` (CLI) or `tier: "open"` (MCP)
  explicitly. The doctrine that a derived note carries the
  most-restrictive tier of its sources still applies — it is now the
  calling agent's job to determine that tier and pass it, not the
  tool's. Vaults already scaffolded keep the old (permissive-reading)
  `save.SKILL.md`/`privacy-tier.SKILL.md` text until `creek skills
  sync` or a re-`creek init` re-deploys the updated templates.
- **`creek skills` is now a typer subapp** (FEAT-019). Replace existing
  invocations:
  - `creek skills --generate --vault <vault>`
    → `creek skills generate --generate --vault <vault>`
  - The new sibling `creek skills sync --vault <vault>` re-deploys the
    canonical schema-skill tree from `creek-tools/creek/templates/skills/`
    into `<vault>/00-Creek-Meta/Skills/`. Pass `--force` to overwrite
    locally-modified files.
- **`creek init --vault <path>` is required** (FEAT-019). The previous
  default of "current directory" is gone. `creek init` also refuses
  paths inside a git repository by default; pass `--allow-in-repo` to
  override.

### Added

- **`creek.reflect` returns the compiled layer nearest the entry — contract
  `0.9.0` (#873).** An `ok`/`empty` reflection may now carry two **optional**,
  bounded fields: `related_praxis` (≤3 `{title, praxis_type, status, excerpt}`)
  and `related_eddies` (≤2 `{title, description, fragment_count, formed}`).
  Published on both surfaces — the MCP tool and `POST /v1/reflections`, whose
  `ReflectionResponse` schema and success fixture move with it. Both keys are
  **absent**, never present-and-empty, when nothing qualifies, so a `0.8`
  consumer's ordinary reflection is byte-identical and `SUPPORTED_CONTRACT_MINORS`
  widens rather than shifts.

  The admission rule is stricter than for a fragment, and it is the point of the
  change rather than a caveat on it. An eddy page's `description` and
  `fragment_count` are synthesised *from its members*, and a praxis page is
  distilled from the fragments its `derived_from` names; neither carries a
  `privacy_tier` of its own, so there is nothing on the page to rank. The new
  `creek_mcp/compiled_pages.py` therefore publishes a page only when **every**
  fragment it was compiled from resolves on disk *and* is within the caller's
  `privacy_tier_ceiling` — and withholds any page whose provenance it cannot
  enumerate in full (an eddy whose `fragment_count` exceeds its findable
  members, a praxis naming a vanished id, a page declaring no sources at all).
  "No provenance" is never read as "no sources". Selection reuses the fragment
  ids the grounding pass already resolved, so there is no second embedding sweep
  and no new egress path.

- `creek init --refresh` re-copies canonical templates (ontology spec,
  AGENTS.md, schema-skill tree, folder scaffold) into an existing vault
  without touching user data or `creek_config.yaml`.
- Canonical templates live under `creek-tools/creek/templates/{vault,
  skills,AGENTS.md}`. The ontology spec lives at repo-level
  `docs/Ontology/creek_ontology_agent_prompt.md`.
- **Freshness-aware embeddings cache** (INC-006): `creek link --method
  embeddings` now persists vectors to
  `<vault>/00-Creek-Meta/embeddings.parquet` with `(fragment_id,
  content_hash, model_name, embedding, computed_at)`. Re-runs reuse rows
  whose `content_hash` matches the current fragment text and recompute
  the rest, so a 1k-fragment vault re-links in seconds. Switching
  `embeddings.model` invalidates the cache automatically; `--rebuild`
  still forces a full recompute from scratch. Existing `embeddings.npz`
  files are ignored and can be deleted.

### Removed

- **Four report surfaces on the redaction classes, none of which had a
  production caller** (#1338): `RedactionScanner.scan_directory`,
  `.generate_report`, `.generate_json_report`, and
  `Redactor.log_redactions`. Every shipped read path — `creek redact
  --scan/--apply/--review`, the `creek.redact.scan` MCP tool, and
  `creek process` — reaches the scanner through `scan_batch`, and the
  console report through `generate_markdown_summary`. Deleting
  `log_redactions` also retires the only code that wrote the session
  salt to disk in cleartext beside the hashes it exists to protect.
  There is no longer a JSON report surface; the typed statistics
  contract on the MCP tool is unaffected (see #1292).

- The repo no longer ships empty placeholder directories for
  `01-Fragments/` … `10-Liminal/` or `00-Creek-Meta/`. User vault
  content lives outside this repository (default suggested location:
  `~/Obsidian/Creek-Vault/`, scaffolded by `creek init`).

### Fixed

- **The redaction docs promised a queue that has never existed and an
  `--apply` that is reversible; neither was true** (#1338). On a
  redaction tool the docs are the operator's contract, and the most
  dangerous line — "applying is reversible if you keep the queue
  around" — invited an unrecoverable in-place rewrite of an
  irreplaceable export with no backup. Corrected against executed
  behaviour: `--scan` writes **no files at all** (`--report` prints to
  the console; the dot-directory and JSON queue the docs named under
  `<source>` were never created by any code path), `--apply` re-scans
  from scratch and rewrites matching
  files **in place with no undo**, and `--review` lists **every**
  finding rather than filtering on a `pending_review` marker — nothing
  in the codebase reads that key. `docs/redaction.md` gains an explicit
  back-up-first warning covering the parts that are worse than merely
  irreversible: structured `.yaml`/`.json` files are in scope, and a
  vault-wide run still rewrites `creek_config.yaml` (#1398, not fixed
  here). The sample report table no longer shows an `Excerpt` column of
  secret text, which contradicted the module's own never-store
  invariant. Also corrected: the previous `#1398` note claimed
  `false_positive_allowlist` entries get redacted — they are
  exact-string exempt and are the one value in that file the rewrite
  cannot touch; the real hazard is `exclude_patterns`, custom
  `patterns`, paths and comments. Two new suites pin the prose to
  behaviour so it cannot drift back.
- **The drift guard that refuses to overwrite a hand-edited skill file
  now covers medium contracts too, not just schema skills, and it now
  lives inside the shared deployment primitive itself** (#1306).
  `<vault>/00-Creek-Meta/Skills/mediums/*.MEDIUM.md` is refused on
  divergence exactly like `*.SKILL.md`; because the guard moved into
  the deploy primitive, `creek init --refresh` is covered by the same
  check, and a refusal there is atomic — nothing at all is deployed,
  not just the skill tree. `--force` no longer silently discards the
  operator's edits: it now preserves the local version alongside as
  `<name>.bak` (e.g. `mediums/essay.MEDIUM.md.bak`) before
  overwriting.
- **`creek link --method embeddings` now reports what actually happened on
  disk, not an assumption about it** (#1337). The summary line used to claim
  a single "N fragment(s) embedded" count regardless of cache state; it now
  reports three independent numbers — `scanned` (fragments loaded from the
  vault), `computed` (vectors the model actually produced this run, i.e.
  cache misses only — legitimately zero on a warm cache), and `cached`
  (rows in `00-Creek-Meta/embeddings.parquet` after the run). When nothing
  reached disk — an empty vault, or a cache write that failed — the line
  now says `no vectors written` instead of silently repeating a stale count;
  a failed cache write was previously swallowed into a log line only, with
  the CLI still exiting 0. Separately, counting resonance edges no longer
  materializes a `Resonance` object per pair
  (`EmbeddingLinker.count_resonances` walks the same traversal as
  `find_resonances` without allocating them), making peak memory
  independent of edge count. Resonance edges are still not persisted, and
  the `embeddings.parquet` schema is unchanged (`fragment_id`,
  `content_hash`, `model_name`, `embedding`, `computed_at`) — no on-disk
  format changed, and no migration is needed.
