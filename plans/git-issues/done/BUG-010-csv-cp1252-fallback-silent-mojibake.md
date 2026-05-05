# BUG-010: CSV ingestor's cp1252 fallback silently produces mojibake for non-Western encodings

**Severity:** Low
**Category:** BUG
**Estimated complexity:** S (≤2h)
**Parallelizable with peers in same category:** yes
**Discovered by:** Reading dimension 1; confirmed by parallel agent

## Files affected
- `creek/ingest/spreadsheets.py:187, 207-218`

## Dependencies
None.

## Blockers
None.

## Reproduction
Create a CSV in Shift-JIS encoding (or ISO-8859-5 / GBK / etc.). Run `creek ingest --type spreadsheet`. The ingestor will accept the file; the resulting markdown table will contain garbled text. No warning is emitted.

## Analysis

`docs/ingestion.md` line 47:
> CSV decoding probes `utf-8-sig` first (handles BOM + plain UTF-8), then falls back to `cp1252` (handles legacy Excel exports). You will not get a `UnicodeDecodeError`.

The "you will not get a UnicodeDecodeError" guarantee is technically true because cp1252 is a single-byte encoding with no invalid sequences — every byte decodes to *some* character. But this means non-Western files decode silently into garbage. The user's first sign that ingestion misbehaved will be unreadable fragments in the vault, well after ingestion finished.

Confidence: verified.

## Proposed remediation

Add `chardet` (already a dependency) probe before falling back to cp1252. Order:
1. utf-8-sig (handles BOM + plain UTF-8)
2. `chardet.detect` over a sample buffer; if confidence > 0.7, decode with that
3. cp1252 fallback **with a warning**: `logger.warning("File %s decoded as cp1252 with no encoding probe match; check fragment %s for mojibake.", path, frag_id)`

Alternative: refuse to ingest and add the file to the review queue if probe confidence is low. Stricter but better for data integrity.

## Acceptance criteria

- A Shift-JIS CSV ingest emits a warning that includes the file path.
- A UTF-8 (with or without BOM) CSV ingests with no warning.
- A cp1252-encoded CSV ingests with no warning (legacy Excel still "just works").
- Documentation reflects the actual probe order.

## References
- `creek-tools/docs/ingestion.md` line 47
- `creek/ingest/spreadsheets.py:187`
