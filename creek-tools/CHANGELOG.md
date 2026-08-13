# Changelog

All notable changes to `creek-tools` are documented here. Versioning is
loose during pre-1.0 development; the headings below track design-trace
IDs (`FEAT-*`, `INC-*`, `BUG-*`, …) embedded in commit messages and
inline code annotations. The original `plans/git-issues/` directory of
long-form spec files was retired in #243; use `git log --grep='<ID>'`
to locate the originating commit for any reference below.

## Unreleased

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

### Breaking changes

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
