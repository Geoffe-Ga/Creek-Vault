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
- **Path-traversal guard.** `creek redact --apply` and `--review`
  refuse to follow symlinks that escape the source/vault root
  (SEC-003).
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
- **Anti-forensic guarantees.** `creek purge vault` and
  `creek gdrive --revoke` do best-effort secure-erase passes (write
  zeros, then unlink). Modern SSDs and copy-on-write filesystems
  (APFS, btrfs, ZFS) cannot guarantee that the original bytes are
  unrecoverable from raw flash.
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
- **SEC-003** — Symlink refusal in redaction (resolved)
- **SEC-004** — Prompt injection hardening (resolved)
- **SEC-005** — Audit log tamper-evidence
- **SEC-006** — Privacy-tier enforcement in mine/draft
- **SEC-008** — OAuth token hygiene (resolved)
- **OPS-002** — Non-interactive purge refusal (resolved)
