# Redaction

`creek redact` is the **first** thing you run on any new export. It scans for secrets, API keys, and PII before they enter your vault — and once they've entered, it can scrub them out.

> Redaction is one defensive layer; it does not encrypt the vault and it cannot detect every secret format. Read the [threat model](security/threat-model.md) for the full picture of what Creek does and does not protect against.

This page covers the `creek redact` CLI. For the read-only MCP tool version of the scan (`creek.redact.scan`, the one CrawDad's Discord safety pass calls), see [mcp.md](mcp.md).

## The three modes

`creek redact` is dispatched by exactly one of `--scan`, `--apply`, or `--review`. Mixing them is an error.

| Mode      | Reads from              | Writes to                                       | Purpose |
|-----------|--------------------------|-------------------------------------------------|---------|
| `--scan`  | `--source`               | nothing — `--report` prints to the console      | Find sensitive content. |
| `--apply` | `--source` (re-scanned)  | the source files, in place, plus the audit log  | Replace matches with placeholders. |
| `--review`| `--vault`                | stdout                                          | Render the vault's review queue. |

## Workflow

```bash
# 1. Scan the export; --report prints a markdown summary to the console.
creek redact --scan --source ~/exports/journal --report

# 2. Preview what --apply would change, without touching anything.
creek redact --apply --source ~/exports/journal --dry-run

# 3. Commit the changes (skips confirmation with -y). Back up first — see
#    the warning under "What `--apply` does" below.
creek redact --apply --source ~/exports/journal -y
```

## What it scans for

The pattern set lives in `creek.redact.patterns.PATTERN_METADATA` and currently covers:

- API keys: AWS access keys (`AKIA…`), Slack tokens (`xox[bpsa]-…`), JWTs (`eyJ…`).
- GitHub tokens: classic (`gh[pousr]_…`) and fine-grained (`github_pat_…`).
- Discord bot tokens (three dot-separated base64url segments).
- Stripe keys (`sk_live_…`, `sk_test_…`, `pk_…`, `rk_…`).
- Anthropic keys (`sk-ant-…`) and OpenAI project keys (`sk-proj-…`).
- Generic high-entropy strings (≥20 chars). A run is flagged when its *whole-run* Shannon entropy clears the `min_confidence`-derived threshold, or when any contiguous 20-character window of it does — so a secret can't be hidden by gluing predictable filler alongside it.
- Email addresses and `email:password` combos.
- US phone numbers.
- Social Security numbers.
- Credit card numbers (Luhn-validated post-filter).
- IPv4 addresses (with octet-range validation).
- IPv6 addresses (full, shortened, and IPv4-mapped forms).
- Private-key headers (`-----BEGIN ... PRIVATE KEY-----`).
- Bearer tokens, env-secret assignments, AWS secret keys.

Configuration in `RedactionConfig`:

| Field                       | Effect |
|-----------------------------|--------|
| `enabled`                   | Master switch. |
| `dry_run`                   | When `true`, `--apply` plans but never modifies a source file. It still appends dry-run-marked entries to the audit log. |
| `custom_patterns`           | Extra regex name → pattern map merged with built-ins. |
| `false_positive_allowlist`  | Strings whose presence cancels a match (test fixtures, sample keys). |
| `supported_extensions`      | File extensions the scanner walks. |
| `exclude_patterns`          | Path/dir name fragments to skip (`.git`, `node_modules`). |
| `min_confidence`            | Generic high-entropy threshold; `0.0` flags any base64url-ish ≥20 chars, `1.0` requires near-random. Default `0.6` (3.7 bits/char), governing both the whole-run and the sub-run window gate — see [What `--apply` does](#what---apply-does). |
| `replacement_template`      | Marker template used by `--apply`; must contain `{name}`. Default `[REDACTED:{name}]`. |

## What the report looks like

`--scan --report` renders a markdown summary to the console — it writes no
files anywhere. One table per file:

```markdown
## /journals/2026-04-12.md

| Line | Type           | Severity |
|------|----------------|----------|
|   42 | aws_access_key | critical |
|   88 | email          | medium   |
```

The table never shows the matched text — only the line number, the pattern
name, and its severity. This is deliberate, and it follows from the scanner's
core invariant: a finding carries a salted SHA-256 hash of what matched, never
the text, so this report has no matched content available to print.

That invariant is about what the scanner *stores*. It is not a promise that no
Creek command ever puts a secret on your screen: `--review` deliberately quotes
the surrounding source lines — see [Reviewing inside the vault](#reviewing-inside-the-vault).

## What `--apply` does

### `--apply` rewrites your files in place, and there is no undo

`--apply` does not consume anything `--scan` left behind — there is nothing
to consume. It re-scans the source from scratch and rewrites every matching
file in place, replacing each matched span with the marker. There is no
queue, no `.bak`, and no restore path: the original bytes are gone once the
write lands. The audit trail at `<vault>/00-Creek-Meta/audit/redact.jsonl`
records *that* a file was rewritten and which pattern names fired — never
the original text, never an offset — so it is a forensic record, not an
undo log.

**Back up your source tree before you run `--apply`.** Copy it, commit it,
or run `--dry-run` first and read the output — but assume the run is
final.

It rewrites more than fragment bodies. Every file whose extension is in
`supported_extensions` is in scope, including structured `.yaml` and
`.json` files, where a replacement marker can leave the file invalid or
quietly change its meaning. Three paths are excluded unconditionally —
`00-Creek-Meta/audit/`, the legacy purge log, and
`<vault>/00-Creek-Meta/creek_config.yaml` (#1398) — and nothing else is.
That exclusion does **not** require `--vault`: the roots it is anchored
on are derived from the tree `--source` names as well, so the workflow
above (which never passes `--vault`) is protected.
Your own structured files are still in scope, so point `--source` at the
narrowest tree that needs cleaning rather than at a whole vault.

For every match found by the scan:

1. Replaces the match with the marker rendered from
   `RedactionConfig.replacement_template` (default `[REDACTED:{name}]`;
   `{name}` is the pattern key — e.g. `credit_card`, `ipv4`,
   `high_entropy_string`). Matches that truly overlap — including ones the
   generic high-entropy detector finds — are unioned into a single region
   and replaced as one, labelled with the most severe contributing
   pattern's name; an AWS key with a high-entropy tail is removed whole as
   `[REDACTED:api_key]` rather than leaving the tail in cleartext. Before
   merging, any match whose start or end falls strictly inside a
   contiguous high-entropy candidate run (base64url-ish, 20+ characters)
   is widened out to that run's edge, so a match can never bisect one
   token. The entropy detector itself is layered: a candidate run is
   flagged when its *whole-run* Shannon entropy clears the
   `min_confidence`-derived threshold, or when any contiguous
   20-character window of it does (issue #942) — so gluing predictable
   filler to a genuine secret can no longer drag the whole-run average
   below the bar and hide it from either `--scan` or `--apply`. Snapping
   is the threshold-independent backstop beneath both entropy gates, for
   runs that clear neither: an AWS example key followed by fourteen
   repeats of a single character measures 3.14 bits/char whole-run and
   has no clearing 20-character window, so the entropy detector
   contributes no span at all — only snapping keeps a regex match that
   covers just the key half from leaving that tail in cleartext (issue
   #909). Strings on `false_positive_allowlist` are exempt from this
   widening — a regex match inside such a string still redacts only its
   own span.
2. Refuses to write through symlinks (path-traversal guard): before any
   file is read or rewritten the source tree is walked and the run is
   aborted if any descendant symlink resolves outside the source root.
   The same guard is applied to `creek redact --review`.

Because of that widening, `--apply` may redact slightly **more** than
`--scan` reported — the marker can extend past the reported match to the
end of the surrounding token. Concretely: a 20-character API key glued
directly to a longer token is removed whole as a single
`[REDACTED:api_key]`, not left half-redacted. This is deliberate,
fail-closed behaviour — a missed secret is unrecoverable once written,
whereas over-redaction is visible in the output and fixable by adding the
token to `false_positive_allowlist`.

The sub-run window gate has a measured over-redaction cost, from a sweep
of this repository (~9.9 MB of `.md`, `.py`, `.txt`, `.yaml`, `.toml`,
`.json`; 29,533 candidate runs, 9,825 flagged before the gate): it flags
**+928 runs (+9.4%)**, concentrated in code identifiers (`.py`: +876);
ordinary markdown prose rises only **+3.0%** (+31 of 1,028). Every newly
flagged run contains a 20-character substring the detector already
flagged when that substring stood alone, so this is the existing
false-positive rate applied consistently, not a new class of false
positive. By `min_confidence`: `0.0` → +0%, `0.4` → +1.1%, `0.6`
(default) → +9.4%, `0.8` → +1.2%, `1.0` → +0%.

This cost is the accepted side of the same fail-closed trade-off: the
alternative considered and rejected was restricting the window scan to
runs of 40+ characters, which measured a **0%** detection rate for a
full 20-character secret hidden behind a 12–19 character masker — a
constructible, total, silent leak.

`--dry-run` performs the same scan and shows what would change, without modifying any source file — useful for sanity-checking a big batch before committing. It does still write dry-run-marked audit entries; see [the audit trail](#the-audit-trail).

## Reviewing inside the vault

Once a fragment has landed in the vault, use `creek redact --review --vault <path>` to re-scan the whole tree and render a **Redaction Review Queue** — every finding, with surrounding context and a checkbox, so a human can triage true positives from false alarms. It is not filtered by any frontmatter field; it lists everything the scan turns up, every time you run it. Nothing is written: the queue is printed to stdout and is gone when your terminal scrollback is.

The context block quotes the file's own lines **verbatim**, which means it prints the matched secret in cleartext. That is the point — you cannot tell a real key from a git hash without seeing it — but it makes `--review` output unsafe to paste into a ticket, a chat, or an LLM prompt without reading it first.

OCR ingestion (`creek/ingest/images.py`) separately tags a low-confidence fragment `review: pending_review` in its frontmatter as a marker for humans. No command reads or filters on that key — it is informational only, not a queue.

## Right-to-be-forgotten

Redaction sanitises *content*; it doesn't delete the fragment. If you need a fragment, source, or date range gone entirely, that's `creek purge` — see [cleaning-and-purge.md](cleaning-and-purge.md).

## Recommended cadence

- **Always** scan before `creek ingest` on any new export.
- **Re-scan** the vault after every classification pass, especially if you've turned on the LLM path — it can occasionally surface PII the rules missed.
- **Audit** the report monthly. See [the audit trail](#the-audit-trail) below for what `<vault>/00-Creek-Meta/audit/redact.jsonl` records and, just as importantly, what it does not.

## The audit trail

Every `creek redact --apply` invocation — including `--dry-run`, whose entries
are all marked `dry_run: true` — writes a three-phase record to
`<vault>/00-Creek-Meta/audit/redact.jsonl`. The log shares the same hash-chain
integrity as the purge audit log.

| `phase`   | Written when | Carries |
|-----------|--------------|---------|
| `intent`  | After you confirm, **before the first file is rewritten** | `files`: every candidate path the run is about to touch |
| `file`    | Immediately after **that file's own** atomic write | `source_path`, `pattern_names`, `match_counts` |
| `outcome` | Last | `status`: `complete` or `partial`, plus `failure_reason` on a partial |

All three share one `operation_id`, which is how you group the lines of a single
run — and the only way to tell one three-file preview from three separate
one-file previews.

### What you may conclude from it, and no more

The per-file entry is appended next to the write it records, not batched after
the run, so a run that dies partway still names what it destroyed. Concretely:

- every file carrying a `file` entry **was** rewritten;
- **at most one** further file may have been rewritten without its record — the
  one in flight when the run died;
- nothing outside the `intent` entry's `files` list was touched.

That one-file window is the honest bound. Do not read the absence of a `file`
entry as proof a file was untouched unless the run also has an `outcome` entry.

An `intent` line with no `outcome` line means the run did not finish — **or**
that the outcome write itself failed. The outcome append is deliberately
best-effort so it can never displace the error it is reporting; when it fails,
the CLI prints a warning naming the audit log. A successful run whose outcome
line was lost is therefore indistinguishable in the log from an aborted one, and
the console warning is the only thing that separates them.

`failure_reason` is the exception **type name only** (`OSError`,
`KeyboardInterrupt`), never the message. An `OSError` message embeds the
offending path, and in a redaction workflow filenames routinely carry the very
secrets being redacted. The type is the forensic value; the message is the leak.

`match_counts` is **what the scan found in this file** — not a count of
substitutions actually performed. Scan/apply parity is a separate open gap
(#900, #946).

### Durability

`AuditLog.append` fsyncs inside its flock window, so the `intent` line's bytes
are durable before the first rewrite. Not claimed: that a freshly created log's
parent directory entry is fsynced, or that the atomic write's `os.replace` is.
Both residuals fail safe — intent present, rewrite lost.

### What `--apply` will not rewrite

The audit trail lives inside the tree `--apply` walks, and `exclude_patterns`
says nothing about `00-Creek-Meta`. Left alone, a vault-wide run rewrote its own
history — and `verify()` still passed, because the entries appended afterwards
re-anchored the chain onto the mutated line.

`<vault>/00-Creek-Meta/creek_config.yaml` had the same problem for a
different reason (#1398): `.yaml` *is* in the default
`supported_extensions`, so a vault-wide apply rewrote the config that
governs the next run. `false_positive_allowlist` entries were the one class
of value it could never touch — the allowlist check is exact-string
membership, so the scanner declines the match before the redactor sees it —
but `exclude_patterns` tokens, custom `patterns`, paths and comments were
all in scope. An entry like `backups-AKIAIOSFODNN7EXAMPLE` became
`[REDACTED:high_entropy_string]`, and losing an exclusion *widens* what the
next run walks.

So three paths are excluded from the rewrite set **unconditionally** —
independent of `supported_extensions` and `exclude_patterns`:

- the whole of `00-Creek-Meta/audit/`
- the legacy `00-Creek-Meta/Processing-Log/purge-log.json`
- `00-Creek-Meta/creek_config.yaml`

"Unconditionally" covers the *root* the paths are anchored on, not just
the settings. The first cut anchored them on `--vault`, falling back to
`config.vault_path` — which defaults to the **current directory** — so
`creek redact --apply --source <vault> --yes` from outside the vault got
no protection at all, and that is the shape every example in this
document uses. The roots are now taken from `--source` too: every
directory at or above the walked tree that carries a `00-Creek-Meta/`.
Standing anywhere, with or without `--vault`, and including
`--source <vault>/00-Creek-Meta`, the three paths are excluded.

Detection is unaffected: `--scan` and `--review` still report matches there.

Two known residuals:

- A secret that leaked into a *filename* is recorded in `source_path` and can no
  longer be remediated by `creek redact --apply`, because the file holding it is
  now protected. Rewriting a hash-chained log needs a chain-aware operation —
  that is purge-shaped work, tracked in #1397.
- Structured files *you* own are still rewritten in place. A
  replacement marker inside a `.json` or `.yaml` file can break its
  syntax or quietly change its meaning, and there is no undo. Only the
  three paths listed above are protected.

## How `creek process` interacts with redaction

`creek process` is **fail-loud**: when the redaction scan finds any unresolved
matches it raises `RedactionRequiredError` and exits non-zero before
ingestion. The CLI prints an exact remediation hint:

```
Redaction gate: Redaction scan found 17 unresolved match(es) in /tmp/exports.
Run `creek redact --apply --source /tmp/exports` (or set redaction.dry_run:
true to skip this gate) before re-running `creek process`.
```

Two ways to override the gate:

1. **Recommended.** Run `creek redact --apply --source <path>` so the
   replacements happen with operator review, then re-run
   `creek process`.
2. Set `redaction.dry_run: true` in your `creek_config.yaml`. The
   pipeline still scans and logs the matches, but does not abort. This
   is the right setting only when the source is known to be safe — e.g.
   regenerating a vault from already-cleaned exports.

This trades a small ergonomic cost (an extra command) for the guarantee
that `creek process` cannot silently leak secrets into the vault.

A second, narrower refusal fires *before* that gate and takes no waiver. If
any file under the source tree is a symlink whose target resolves outside
that tree, `creek process` raises `SymlinkEscapedSourceError` and exits
non-zero without ingesting anything:

```
Symlink containment: Redaction scan declined to read 1 file(s) under
/tmp/exports: each is a symlink whose target resolves outside that tree,
so the scanner could not check content that ingestion would still have
read. Remove or re-point the offending link(s) under /tmp/exports before
re-running `creek process`.
```

The scan itself did not fail here — it *skipped* the escaping link and
carried on, which is why the pipeline has to refuse on its behalf. A clean
result reached by declining to look reads exactly like a clean tree.

Since #1294 ingestion no longer depends on that refusal. Every ingestor
passes through one containment gate before it discovers anything, so
`creek ingest`, the `creek.ingest` MCP tool and `creek process` all refuse
a source tree containing an escaping symlink — including when
`redaction.enabled: false` skips the scan entirely. The pipeline refusal
above is kept as the earlier, better-informed of the two: it fires before
any ingestor runs and reports how many files the scan declined.
Remediation:

1. **Find the offending links.** Each skip is logged as it happens —
   `Skipping symlink that escapes the scan root: <path>` — naming the link
   as scanned, deliberately never its target. Running
   `creek redact --scan --source <path>` over the same tree reports the
   count as a **Files skipped (escaping symlink)** row in its statistics
   (the row is omitted entirely when nothing was declined).
2. **Remove or re-point each one** so it lands inside the source tree, or
   copy the target's content in so the scanner reads the bytes it is being
   asked to vouch for. Then re-run `creek process`.

Neither override above applies. `redaction.dry_run: true` does not suppress
this refusal — that setting means "log the matches you found", not "proceed
past a file nobody read". `redaction.enabled: false` does skip the whole
redaction stage, symlink check included; what it no longer skips is
containment, because the ingestors enforce that themselves (#1294). Turning
redaction off costs you the PII scan, not the path-traversal guard.

The check is deliberately narrow. A symlink whose target stays *inside* the
source tree is still admitted, and is still scanned **unresolved** — under
the path it was reached by rather than its target's — so findings keep
being reported at the path the operator actually has.

### A named `--vault` must stay inside its own parent

`--vault` is a *named* path rather than a discovered one, and every mode
refuses one that is a symlink whose target escapes the link's own parent.
`--apply` and `--review` have refused since #1293; `--scan` joined them in
#1359.

`--scan` looked like it could be exempt: it writes nothing, and `--vault`
there does exactly one thing — locate
`<vault>/00-Creek-Meta/creek_config.yaml`. That reading turned out to be
wrong. An out-of-tree config is not passively read; it is a complete off
switch. A scan of a file holding one email address and one API key reports
**zero** findings if the config it is handed narrows `supported_extensions`
past the source's extension, names the source directory in
`exclude_patterns`, or lists the matched strings in
`false_positive_allowlist`. (`enabled: false` is the one setting that
changes nothing: the explicit CLI mode does not consult it, so it fails
safe.) Refusing costs you a one-line correction; the alternative was a
silent "no findings" on the command you run precisely because you are
worried.

```
$ creek redact --scan --source ./notes --vault ~/linkvault
Refusing to follow symlink that escapes the vault root: /Users/me/linkvault
```

The banner names the path you supplied and never its target — disclosing
the target is the existence oracle the containment work exists to close.
Pass the resolved path instead (`--vault /Volumes/Backup/vault`), or drop
`--vault` and let config discovery fall back to `CREEK_CONFIG` or the
working directory.

This is **not** the skip-and-count contract, and does not dent it.
Skip-and-count is about `--source` — the tree being examined — where one
stray link must never disable the whole safety pass. A `--source` tree
containing an escaping link is still scanned in full, the link is still
declined, the exit code is still 0, and the skip is still named in the
statistics. A `--vault` symlink that stays inside its own parent
(`<root>/linkvault -> <root>/realvault`) is still admitted.
