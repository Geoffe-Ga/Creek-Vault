# Redaction

`creek redact` is the **first** thing you run on any new export. It scans for secrets, API keys, and PII before they enter your vault — and once they've entered, it can scrub them out.

## The three modes

`creek redact` is dispatched by exactly one of `--scan`, `--apply`, or `--review`. Mixing them is an error.

| Mode      | Reads from        | Writes to                          | Purpose |
|-----------|-------------------|------------------------------------|---------|
| `--scan`  | `--source`        | `<source>/.creek-redactions/queue.json` (+ optional `report.md`) | Find sensitive content. |
| `--apply` | the queue         | the source files (and the queue)   | Replace matches with placeholders. |
| `--review`| `--vault`         | stdout                             | Render the vault's review queue. |

## Workflow

```bash
# 1. Scan the export.
creek redact --scan --source ~/exports/journal --report

# 2. Read the report.
$EDITOR ~/exports/journal/.creek-redactions/report.md

# 3. Preview what would change.
creek redact --apply --source ~/exports/journal --dry-run

# 4. Commit the changes (skips confirmation with -y).
creek redact --apply --source ~/exports/journal -y
```

## What it scans for

The pattern set lives in `creek.redact.patterns.REDACTION_PATTERNS` and currently covers:

- API keys (AWS, GitHub, Anthropic, OpenAI, Slack tokens, generic high-entropy strings).
- Email addresses.
- Phone numbers (US + international).
- Social Security numbers.
- Credit card numbers (Luhn-validated).
- IP addresses (IPv4 + IPv6).
- Common private-key headers (`-----BEGIN ... PRIVATE KEY-----`).

Configuration in `RedactionConfig`:

| Field            | Effect |
|------------------|--------|
| `enabled_patterns` | Allow-list of pattern names. Empty list means *all*. |
| `excluded_globs`   | Filename patterns to skip (defaults exclude `.git/`, `node_modules/`, `__pycache__/`). |
| `allow_list`       | Substrings whose presence cancels a match (e.g. test fixtures, sample API keys). |
| `min_confidence`   | Generic high-entropy threshold below which a match is suppressed. |

## Output formats

`--scan --report` produces a markdown report with one section per file:

```markdown
## /journals/2026-04-12.md

| Line | Pattern        | Excerpt           |
|------|----------------|-------------------|
|   42 | aws_access_key | AKIAIOSFODNN7…    |
|   88 | email          | sgsg@example.com  |
```

The structured queue at `<source>/.creek-redactions/queue.json` is what `--apply` actually consumes. It records the offset, the pattern that matched, and the replacement string — so applying is reversible if you keep the queue around.

## What `--apply` does

For every queued match:

1. Replaces the match with `[REDACTED:<pattern_name>]` (configurable via `RedactionConfig.replacement_template`).
2. Marks the queue entry as `applied: true` and stamps the timestamp.
3. Refuses to write through symlinks (path-traversal guard).

`--dry-run` walks the queue without modifying any source file — useful for sanity-checking a big batch before committing.

## Reviewing inside the vault

Once a fragment has landed in the vault, the source-side queue is no longer the right tool. Use `creek redact --review --vault <path>` to print every fragment whose `redaction.status` is `pending_review` (typically because a match landed in non-redactable content like image OCR text and needs a human decision).

## Right-to-be-forgotten

Redaction sanitises *content*; it doesn't delete the fragment. If you need a fragment, source, or date range gone entirely, that's `creek purge` — see [cleaning-and-purge.md](cleaning-and-purge.md).

## Recommended cadence

- **Always** scan before `creek ingest` on any new export.
- **Re-scan** the vault after every classification pass, especially if you've turned on the LLM path — it can occasionally surface PII the rules missed.
- **Audit** the report monthly. Every `creek redact --apply` invocation appends one JSONL entry per touched file to `<vault>/00-Creek-Meta/audit/redact.jsonl` — including dry-runs, marked with `dry_run: true`. The log shares the same hash-chain integrity as the purge audit log; see [cleaning-and-purge.md → Audit trail](cleaning-and-purge.md#audit-trail) for the full schema.

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
