# INC-012: README claims "12 source platforms" but only 11 ingestors are registered; "other" is a SourcePlatform but not an ingestor

**Severity:** Low
**Category:** INC
**Estimated complexity:** S (≤2h)
**Parallelizable with peers in same category:** yes
**Discovered by:** Dimension 5

## Files affected
- `creek-tools/README.md:141-145`
- `creek/ingest/__init__.py` — `INGESTOR_REGISTRY`
- `creek/models.py:168-182` — `SourcePlatform` enum

## Dependencies
None.

## Reproduction
```bash
grep -E "^class \w+Ingestor" creek/ingest/*.py | wc -l
# 10 (Claude, ChatGPT, Discord, Code, Markdown, Document, Spreadsheet, Presentation, Image, Generic)
```

`gdrive` is a downloader, not an `Ingestor`; `other` is an enum value with no parser.

## Analysis

README:
> The ingestion pipeline currently supports **12** source platforms, each backed by a registered `Ingestor`:  
> `claude`, `chatgpt`, `discord`, `gdrive`, `code`, `documents` (.docx / .pdf), `markdown`, `spreadsheet` (.xlsx / .csv), `presentation` (.pptx), `images` (with OCR), `generic` (fallback for unknown text), and `other`.

- `gdrive` is a downloader (`GoogleDriveDownloader`); the actual ingestion of downloaded files is routed back through `documents`/`spreadsheet`/etc. via `route_to_ingestor` in `creek/ingest/gdrive.py`.
- `other` is a `SourcePlatform` enum value but no `OtherIngestor` exists.

The doc is simply mis-counting. Either (a) make the README accurate ("11 source ingestors plus a Drive downloader"), or (b) implement an explicit fallback `OtherIngestor` that just routes to `generic` and add `gdrive` as an `Ingestor` subclass that wraps the downloader.

Confidence: verified.

## Proposed remediation

Update the README sentence to match reality. Suggested wording: "10 source ingestors (`claude`, `chatgpt`, `discord`, `code`, `documents`, `markdown`, `spreadsheet`, `presentation`, `images`, `generic`) plus a read-only Google Drive downloader that routes mirrored files back through the appropriate ingestor."

Drop the `OTHER` enum value if no ingestor uses it (or document its purpose if there's one — could be a placeholder for future plugins).

## Acceptance criteria

- README count matches the registry size.
- The CLI `ingest --type X` table in `docs/ingestion.md:8-22` is consistent with the registry.
- A test asserts `len(INGESTOR_REGISTRY) == <docs claim>`.

## References
- `creek-tools/README.md:141-145`
- `creek/ingest/__init__.py`
- `creek/models.py:168-182`
