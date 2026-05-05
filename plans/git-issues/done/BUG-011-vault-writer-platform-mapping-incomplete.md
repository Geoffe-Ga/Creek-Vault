# BUG-011: `_PLATFORM_SUBFOLDER` doesn't cover every `SourcePlatform`; non-mapped fragments silently land in `Unsorted/`

**Severity:** Medium
**Category:** BUG
**Estimated complexity:** S (≤2h)
**Parallelizable with peers in same category:** yes
**Discovered by:** Reading dimension 1 — `creek/vault/writer.py:43-50`

## Files affected
- `creek/vault/writer.py:43-50`
- `creek/models.py:168-182` (`SourcePlatform` enum)

## Dependencies
None. Pairs with INC-013 (clean modules undocumented).

## Reproduction
```python
from creek.vault.writer import _PLATFORM_SUBFOLDER
from creek.models import SourcePlatform

# Mapped: claude, chatgpt, discord, essay, journal, code -> 6 of 12
# Missing: email, document, image_ocr, spreadsheet, presentation, other
print(set(SourcePlatform) - set(SourcePlatform(k) for k in _PLATFORM_SUBFOLDER.keys()))
# {SourcePlatform.EMAIL, SourcePlatform.DOCUMENT, SourcePlatform.IMAGE_OCR, ...}
```

A spreadsheet ingest writes its fragment to `01-Fragments/Unsorted/` instead of a more meaningful subfolder. The same is true of presentations, OCR images, documents, emails, and `OTHER`.

## Analysis

`_PLATFORM_SUBFOLDER` only covers 6 of the 12 documented source platforms. The fallback at `_write_model` line 165 routes everything else to `Unsorted/`. This means after a multi-source ingestion, half the vault lives in `Unsorted/` with nothing visibly distinguishing a Discord message from an OCR'd screenshot.

The ontology spec §4.1 documents `01-Fragments/Conversations`, `Messages`, `Writing`, `Journal`, `Technical`, `Unsorted`. Documents/spreadsheets/presentations/images/email don't fit any of those subfolders — but the user-experience expectation set by the spec is that fragments are organised by source kind, not "everything I didn't classify yet."

## Proposed remediation

Decide on subfolder mapping for the missing platforms:
- `DOCUMENT` → `Writing` (or `Documents`?)
- `SPREADSHEET` → `Data` (new subfolder, document in spec)
- `PRESENTATION` → `Decks` (new subfolder)
- `IMAGE_OCR` → `Images` (new subfolder; or `Screenshots`)
- `EMAIL` → `Messages`
- `OTHER` → `Unsorted` (intentional)

Update `_PLATFORM_SUBFOLDER`. Update spec §4.1 and `docs/getting-started.md` to match.

Alternative: keep mapping minimal but document explicitly which platforms go to which folders.

## Acceptance criteria

- After ingesting `.docx`, `.xlsx`, `.pptx`, an OCR'd image, an `.eml`, each lands in a sensibly-named subfolder.
- `Unsorted/` only contains fragments that were genuinely unsorted, not "we didn't write the mapping."
- A test asserts the mapping is total (every `SourcePlatform` is in `_PLATFORM_SUBFOLDER`).
- Vault structure docs match.

## References
- `creek/vault/writer.py:43-50`
- `creek/models.py:168-182`
- `00-Creek-Meta/Ontology/creek_ontology_agent_prompt.md` §4.1
