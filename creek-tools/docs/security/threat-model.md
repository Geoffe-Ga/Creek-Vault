# Creek Threat Model

**Version:** 1.0

This document is the canonical statement of what Creek defends against
and what it does not. It is intentionally short. It assumes you have
already read the [README](../../README.md) and the
[cleaning-and-purge](../cleaning-and-purge.md) and
[redaction](../redaction.md) docs.

The "current as of" date is whatever git-blame says about this
file's last touch — manual `Last reviewed` annotations rot.

If you read "local-first by default" in the README and inferred
"private," read this file before trusting Creek with intimate journal
content.

## Trust boundaries

| Boundary               | What's on the trusted side          | What's on the untrusted side                      |
|------------------------|--------------------------------------|---------------------------------------------------|
| Local filesystem       | Your user account, the vault dir     | Anyone with code execution as your user           |
| LLM provider (Ollama)  | Local process on your machine        | Nothing — fully local                             |
| LLM provider (Anthropic) | API client + your local code       | Anthropic's servers (transient transit + logs)    |
| Google Drive API       | Read-only OAuth scope; staging dir   | Drive itself, network in transit                  |
| Embedding cache        | Sentence-transformer model on disk   | Anyone who can read the cache directory           |

## Assumed adversaries

The threat model assumes the following classes of adversary, ordered
from most to least likely:

1. **Accidental disclosure.** A backup tool that doesn't honour
   permissions, an editor that uploads on save, a screenshot tool
   that bookmarks the vault dir, a misconfigured `git add -A` that
   commits the wrong file. **In scope.**
2. **Third-party LLM logs.** Any fragment routed through the
   Anthropic provider lands in their logs for an indeterminate
   retention window. The `CREEK_ANTHROPIC_CONSENT` gate exists so
   this is always a deliberate choice. **In scope.**
3. **Careless operator.** A `creek purge vault` typo, a forgotten
   `--dry-run`, a pasted secret. **In scope** — defended via
   non-interactive purge refusal (OPS-002), redaction patterns, and
   atomic file writes.
4. **Local malware running as the same user.** A browser extension or
   an Obsidian plugin that reads `token.json` or the vault. **Out of
   scope** — Creek cannot meaningfully defend against an attacker
   with code execution as you.
5. **Multi-tenant host.** A second Unix user on the same machine.
   `0o600` on the OAuth token is the only defence; everything else is
   `0o644`. **Out of scope** beyond filesystem permissions.
6. **Nation-state-grade adversary.** Forensic disk recovery, side
   channels, supply-chain compromise of dependencies. **Out of
   scope.**

## What is protected

- **API keys are env-only.** `ANTHROPIC_API_KEY` is read from the
  environment, never persisted to config or logs (see
  `creek/classify/llm.py`).
- **OAuth tokens are mode `0o600`** with atomic write semantics
  (`creek/ingest/gdrive.py`). Revocation is supported via
  `creek gdrive --revoke` (see SEC-008).
- **Privacy tiers gate cloud LLM use.** Fragments tagged
  `privacy_tier: intimate` are never sent to remote providers (see
  SEC-006 and the [classification](../classification.md) doc).
- **Redaction patterns** scrub well-known secrets (API keys, SSN-like
  strings, etc.) before fragments are read by any LLM (see
  [redaction](../redaction.md) and SEC-002 for known coverage gaps).
- **Path-traversal guard.** Three walks encounter symlinks and they do
  not all respond the same way. The `creek redact --apply`/`--review`
  *directory* walk refuses outright when a child symlink escapes the
  source/vault root (SEC-003, unchanged). The redaction *scan* walk
  (`RedactionScanner.scan_batch`, which backs `creek redact --scan`,
  the `creek.redact.scan` MCP tool, and the pipeline's own redaction
  pass) instead skips an escaping child and counts it, rather than
  refusing the whole scan. The pipeline's redaction pass escalates
  that skip to a hard refusal before it acts on the scan result,
  because `creek.ingest.markdown.MarkdownIngestor` would otherwise
  read the same file again with no symlink guard of its own, turning
  a silent skip into a silent unredacted ingest. #1293 is now closed
  for the CLI's named-path surface: `--apply`/`--review` refuse
  (exit 1) before reading anything through a named symlink —
  including before loading `<vault>/00-Creek-Meta/creek_config.yaml`,
  which was previously read through a symlinked vault. Both paths
  the operator names are contained, not just the scanned one:
  `--apply` also takes a `--vault`, and that one is *written* to —
  the audit record lands in
  `<vault>/00-Creek-Meta/audit/redact.jsonl`, creating the `audit/`
  directory if absent — and since #1308 that write happens *before*
  the first file is rewritten, so the out-of-root write it was
  guarding against now precedes the destruction rather than
  following it, which makes the containment check load-bearing
  earlier, not later. An escaping `--vault` was therefore a second
  out-of-root write, reachable with an entirely innocent `--source`,
  and it put the record of what was touched wherever the link
  pointed. `--scan`
  still skips the named path and counts it under "Files skipped
  (escaping symlink)" rather than refusing the whole pass, keeping
  its distinct skip-and-count contract: `--scan` writes nothing, so
  refusing a whole scan over one bad link would be a denial of
  service on the safety pass itself. The mechanism that was actually
  closed is not what the original report claimed: `--apply
  <symlinked-file>` never "writes through the link" —
  `_atomic_write` ends in `os.replace`, which replaces the *link*,
  not its target. The real defect for a named symlinked file was
  out-of-root read, exfiltration into the tree, and destruction of
  the operator's link — the target's secrets were enumerated and
  printed, and the link was replaced by a regular in-tree file
  holding a redacted copy of out-of-tree content. A named symlinked
  *directory* was a separate, worse failure — a genuine in-place
  out-of-root rewrite, because `is_dir()` follows the link and the
  walk guard then resolved the root to the target, laundering every
  child as "inside." `run_scan` had no symlink guard of any kind.
  The fix for a named symlinked directory is leaf-only, not a
  blanket refusal: it is refused when its target escapes the link's
  own parent, and admitted — with the resolved target used as the
  root everywhere thereafter — when the target stays under that
  parent, so `~/vault -> ~/Dropbox/vault` is still admitted and
  still redacts under `~/Dropbox/vault`. Three residuals remain.
  First, a named path reached through an escaping *ancestor*
  component (e.g. `<root>/linkdir/a.md`, where the leaf is a real
  file but `linkdir` is the escaping link) is still admitted and
  rewritten in place — the same resolve-the-root/lstat-the-leaf
  asymmetry the scanner already documents and relies on to keep a
  `/tmp` -> `/private/tmp` root scannable. Second,
  `Pipeline._run_redaction` (`creek/pipeline.py:478`
  `source_path.rglob("*")` and `:484` `scan_batch`) still traverses
  a directly-named symlinked directory; it is read-only there and
  escalates to a hard refusal via the skip counter rather than
  writing. Third, `--scan`'s own `--vault` is not contained. It is
  used only to locate `creek_config.yaml`, and `--scan` writes
  nothing, so this is a read-escape rather than a write: an
  out-of-tree `creek_config.yaml` is opened, parsed, and used to
  configure the pass (confirmed by pointing it at malformed YAML,
  which surfaces as a parse error from outside the named tree). It
  is deliberately not refused here, because a refusing `--scan` is
  the denial of service the skip-and-count contract exists to
  avoid; tracked separately in #1359. #1294 closed the last of the
  gaps: the ingestor walks had no containment check of any kind, so
  the pipeline refusal was a backstop that a vault with
  `redaction.enabled: false` never reached — and that a caller
  running an ingestor directly (`creek ingest`, the `creek.ingest`
  MCP tool) never reached at all. All eleven registered ingestors
  now pass through one gate,
  `creek._containment.assert_source_contained`, called from
  `Ingestor.ingest` before any discovery walk. Ingest **refuses**
  rather than skipping: it is a write path, so it takes the same
  side of #1293's split as `--apply`/`--review`, and skipping would
  have left the posture depending on whether `redaction.enabled` was
  set. The measured defect was not theoretical — at HEAD a source
  tree containing `nested/link.md -> <outside>/secret.md` produced a
  vault fragment holding the out-of-tree file's text, and
  `CodeIngestor` was worse than the rest: it recurses with
  `iterdir()` + `is_dir()`, which follows symlinks, so it walked an
  entire out-of-tree *subtree* rather than just the named link. The
  gate uses `os.walk(followlinks=False)` and inspects `dirnames` as
  well as `filenames`, which is what closes that. One consequence
  worth recording because it was nearly the opposite of a fix:
  ingestion's refusal must propagate out of `Ingestor.ingest` rather
  than being collected by `_discover_safe`, because an empty
  discovery leaves `run_ingest`'s `seen_keys` empty and
  `tomb_missing_units` then soft-tombs every live ledger key — one
  stray link would have orphaned every fragment previously ingested
  from that source. One further defect surfaced only on Python 3.13
  and is recorded because the mechanism generalises: the predicate
  resolved with `Path.resolve(strict=False)`, whose behaviour on a
  symlink **cycle** pathlib never specified. On 3.11/3.12 it raises
  `RuntimeError`, which the predicate classified as unprovable and so
  refused; on 3.13 `Path.resolve` delegates to `os.path.realpath`,
  which for `strict=False` stops unwinding at the cycle and returns a
  *partially resolved* path. For a cycle that closes back on its own
  starting point that answer erases the hops in between:
  `<root>/a.md -> <outside>/o.md -> <root>/a.md` resolved to
  `<root>/a.md`, an in-root answer for a link whose first hop leaves
  the root, and the tree was admitted. No out-of-root content reached
  the vault, because `open` also fails `ELOOP` — but that is the
  kernel refusing rather than Creek, and it stops holding as soon as
  the cycle is broken at the far end, outside the tree the operator
  named. Resolution now goes through
  `creek._containment._resolved_target`, which uses
  `os.path.realpath(..., strict=True)` — a cycle is `OSError(ELOOP)`
  on every supported version — and treats only `FileNotFoundError` as
  non-fatal, so a dangling link is still judged on its candidate
  location. The verdict is now identical on 3.11, 3.12 and 3.13, and
  the general lesson is that a containment predicate must not take
  its "cannot prove this" signal from an unspecified stdlib
  exception. Residual, unchanged: the escaping-*ancestor*
  component case, which is the leaf-only policy applied
  consistently on all three surfaces. User-visible effect: aliasing an
  external export tree into the vault (`ln -s /Volumes/Export
  ~/vault/inbox`) and naming that alias to `--apply`/`--review` is
  now refused; the workaround is to name the resolved path directly.
  `--scan` on such an alias now reports 0 files scanned with a skip
  row instead of scanning the target.
- **Prompt-injection hardening.** Fragment title and body are
  sanitised before being templated into the LLM classifier prompt;
  responses are strictly validated to reject multi-document YAML and
  undocumented top-level keys (SEC-004). The substring sanitiser
  ``[FENCE]`` / ``[CMT-OPEN]`` / ``[CMT-CLOSE]`` defends only against
  the literal sequences ``---`` / ``<!--`` / ``-->``; an attacker who
  controls fragment content and knows these replacements could craft
  Unicode look-alikes (e.g. fullwidth hyphens, mathematical minus
  signs) that the substring pass would miss. This is acceptable
  because the assumed adversary is "third-party content / careless
  operator," not "sophisticated prompt-injection specialist." The
  strict YAML response validator is the second line of defence.
- **Audit log integrity.** Every purge and redaction-apply writes a
  structured entry to `<vault>/00-Creek-Meta/audit/`. The integrity
  story (hash chaining, tamper-evidence) is the subject of SEC-005;
  treat the current log as a journal, not a trust anchor.
- **The audit trail is not a redaction target.** `creek redact
  --apply` excludes the whole of `<vault>/00-Creek-Meta/audit/` and
  the legacy `Processing-Log/purge-log.json` from its rewrite set
  **unconditionally** — independent of `redaction.supported_extensions`
  and `redaction.exclude_patterns`, both of which are operator-editable
  and neither of which mentioned `00-Creek-Meta`. Before #1308 the log
  survived only because `.jsonl` happened to be absent from the default
  extension list; adding it made a vault-wide run rewrite its own first
  entry, after which `verify()` still passed because the entries
  appended on top re-anchored the chain onto the mutated line. That is
  undetectable tampering with a tamper-evidence log, reachable by
  editing a config field, and the accidental protection is now a
  deliberate one. The legacy purge log is included because
  `PurgeAuditLog._migrate_legacy_if_needed` replays it into the chained
  `purge.jsonl`, which would have chain-*signed* redacted records as
  authentic. Detection is deliberately unchanged: `--scan` and
  `--review` still report matches inside `00-Creek-Meta/audit/`; only
  in-place destruction narrows.
- **A destructive redaction cannot outrun its own record.** The
  per-file audit entry is appended immediately after that file's
  atomic write, and the `intent` entry naming every candidate is
  written before the first one. An unwritable audit destination is
  therefore discovered *before* anything is destroyed rather than
  after, and an abort mid-batch leaves at most one rewritten file
  unrecorded. See [redaction.md → The audit
  trail](../redaction.md#the-audit-trail) for the exact bound.

## What is NOT protected

- **Confidentiality at rest.** Fragments, threads, eddies, and the
  embedding cache are all plaintext on disk. Anyone with read access
  to the vault dir can read everything.
- **Network exposure.** Creek does not run a server. If you place the
  vault on a network share, the share's permissions are the only
  defence.
- **Embedding-cache reverse engineering.** Sentence-transformer
  embeddings can be partially inverted by an attacker who already has
  the cache file. Treat the cache as as-sensitive as the source text.
- **Anti-forensic guarantees — there are none.** `creek purge` and
  `creek gdrive --revoke` do a plain `unlink` / `shutil.rmtree`. There
  is **no** secure-erase pass anywhere in `creek/purge/`: nothing
  overwrites a file before removing it. (This entry previously claimed
  "best-effort secure-erase passes (write zeros, then unlink)". That
  was never true of any released version — #1453.) Deleted content is
  therefore recoverable by ordinary undelete tooling until the blocks
  are reused, and on modern SSDs and copy-on-write filesystems (APFS,
  btrfs, ZFS) even an overwriting implementation could not guarantee
  the original bytes are unrecoverable from raw flash. Full-disk
  encryption is the mitigation; a zero-fill pass would not have been
  one.
- **What survives an erasure.** A purge is scoped to the vault, and
  some of the vault is deliberately kept. `00-Creek-Meta/audit/*.jsonl`
  and the legacy `Processing-Log/purge-log.json` survive by design —
  they are the compliance record, and the whole-vault entry among them
  names every fragment id the vault held. `creek_config.yaml` survives
  because it is the vault marker and holds operator configuration only;
  the `privacy_tier` ratchet is **not** there, being per-fragment
  frontmatter that is destroyed with the fragments, so
  `audit/privacy.jsonl` above is the surviving record that a tier was
  ever raised. Everything else under `00-Creek-Meta/` is destroyed
  deny-by-default (#1453) — **including the `creek init` scaffold**
  (`Ontology/`, `Skills/`, `Templates/`, `Scripts/`), because `Skills/`
  is where operators drop their own skill files and one of those may
  quote a purged fragment. Redeploy with `creek init` / `creek skills
  sync`. The exhaustive keep list, and a test pinning it against the
  code, are in
  [cleaning-and-purge.md](../cleaning-and-purge.md). Outside the vault
  nothing is touched: original source exports, shell history, editor
  swap files, Spotlight/Tracker indexes, Time Machine and other backup
  snapshots, and any git history of the vault all retain content a
  purge removed from the working tree.
- **Multi-tenant safety.** The vault is single-user by design.

## Recommended hygiene

- **Encrypt the disk.** FileVault on macOS, LUKS on Linux,
  BitLocker on Windows. This is the single most important
  mitigation; nothing else in this list matters as much.
- **Gitignore the vault.** If the vault lives in a repository, ensure
  `01-Fragments/`, `creek-skills/`, `.obsidian/`, `token.json`, and
  any `*.env` files are excluded. Better: keep the vault out of any
  repo at all.
- **Audit cloud-sync clients.** iCloud Drive, Dropbox, OneDrive, and
  Google Drive Backup will happily upload your vault by default if it
  lives in their watched directories.
- **Use `creek purge` for right-to-be-forgotten requests.**
  Per-fragment, per-source, per-date-range, or full-vault — see
  [cleaning-and-purge](../cleaning-and-purge.md).
- **Embedding cache hygiene is built into `creek purge`.** Per-fragment,
  per-source, and per-date-range purges drop the matching rows from
  `<vault>/00-Creek-Meta/embeddings.parquet`; `creek purge vault`
  deletes the parquet file outright. The audit log's
  `embeddings_removed` field carries the real row delta — zero when
  the cache had not been built yet, otherwise the exact count
  (GAP-001). If you maintain a *secondary* embedding cache outside
  the vault (e.g. a notebook or experiment store), you still need to
  wipe that one yourself.
- **Rotate the OAuth token after exposure.** Run
  `creek gdrive --revoke` immediately if `token.json` was ever
  copied off the host. See [configuration → google_drive →
  Security considerations](../configuration.md#security-considerations).

## Explicit non-goals

Creek **does not** aim to provide:

- **Multi-user safety.** One vault, one operator.
- **Network-exposed safety.** No daemon, no listener, no API server.
- **DoS resistance.** A malicious 2 GB single file in a source
  directory will use 2 GB of memory; that is acceptable.
- **Nation-state-grade adversary resistance.** Side channels,
  forensic recovery, dependency-chain compromise are out of scope.

## Cross-references

The codebase annotates design-trace work with short IDs: `SEC-*`, `INC-*`, `OPS-*`, `BUG-*`, `FEAT-*`, `TEST-*`. The original `plans/git-issues/` directory of long-form spec files was retired in #243; the IDs survive as commit-message tags and inline code annotations. To locate the originating context for an ID, search the commit history (`git log --grep='SEC-005'` etc.) or the GitHub issue tracker for `geoffe-ga/creek-vault`.

Notable threat-model-adjacent IDs that have shipped or are in flight:

- **SEC-002** — Redaction pattern coverage gaps
- **SEC-003** — Symlink refusal: covers the walked tree, a
  directly-named path (`--apply`/`--review` refuse before reading;
  `--scan` skips and counts), and — since #1294 — every ingestor
  discovery walk, which refuses. One predicate,
  `creek._containment.resolves_within`, serves all three surfaces; the
  escaping-ancestor-component residual is leaf-only and documented
  above (resolved)
- **SEC-004** — Prompt injection hardening (resolved)
- **SEC-005** — Audit log tamper-evidence
- **SEC-006** — Privacy-tier enforcement in mine/draft
- **SEC-008** — OAuth token hygiene (resolved)
- **OPS-002** — Non-interactive purge refusal (resolved)
