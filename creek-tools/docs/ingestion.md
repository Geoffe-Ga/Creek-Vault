# Ingestion

`creek ingest` is the entry point for a single source type. `creek process` chains every applicable ingestor automatically, but you'll typically iterate one type at a time while you tune things.

## Picking a `--type`

Every ingestor is registered in `creek.ingest.INGESTOR_REGISTRY`. The table maps file shapes to the right `--type`:

| `--type`        | What it ingests                                                       | Optional dependency           |
|-----------------|----------------------------------------------------------------------|-------------------------------|
| `claude`        | Claude conversation export ZIP / JSON.                                | none                          |
| `chatgpt`       | ChatGPT conversation export ZIP.                                      | none                          |
| `discord`       | Discord channel exports (DiscordChatExporter HTML or JSON).           | none                          |
| `code`          | A directory tree of source files (`.py`, `.ts`, `.go`, `.md`, …).     | none                          |
| `document`      | `.docx` and `.pdf` documents.                                         | `python-docx`, `pdfminer.six` |
| `markdown`      | Loose `.md` files.                                                    | none                          |
| `spreadsheet`   | `.xlsx` (one fragment per sheet) and `.csv` (one fragment per file).  | `openpyxl` (XLSX only)        |
| `presentation`  | `.pptx` (one fragment per deck; slides become sections).              | `python-pptx`                 |
| `image`         | `.png` / `.jpg` / `.tiff` / `.pdf-page-as-image` via OCR.             | `pytesseract`, `pdf2image`, system `tesseract`, `poppler` |
| `generic`       | Plain-text fallback for unknown extensions.                           | none                          |

If `--type` is omitted, `creek process` picks ingestors by file extension.

`gdrive` is **not** a `--type` (ARCH-001) — it is a downloader. Run `creek gdrive --download --staging <dir>` to mirror Drive files locally, then run `creek ingest --type document --input <dir>` (or `--type spreadsheet`, etc.) against the staged directory. See [Google Drive](#google-drive) below.

## Common patterns

```bash
# Conversation exports.
creek ingest --type claude  --input ~/exports/claude.zip   --vault ~/Obsidian/Creek-Vault
creek ingest --type chatgpt --input ~/exports/chatgpt.zip  --vault ~/Obsidian/Creek-Vault
creek ingest --type discord --input ~/exports/discord.zip  --vault ~/Obsidian/Creek-Vault

# Local document trees.
creek ingest --type documents    --input ~/Documents/journal --vault ~/Obsidian/Creek-Vault
creek ingest --type markdown     --input ~/notes              --vault ~/Obsidian/Creek-Vault
creek ingest --type spreadsheet  --input ~/Downloads/finances --vault ~/Obsidian/Creek-Vault
creek ingest --type presentation --input ~/Talks              --vault ~/Obsidian/Creek-Vault
creek ingest --type images       --input ~/screenshots        --vault ~/Obsidian/Creek-Vault
creek ingest --type code         --input ~/projects/diary     --vault ~/Obsidian/Creek-Vault
```

## Spreadsheet (`spreadsheet`)

- `.xlsx` files emit **one fragment per sheet**; `.csv` files are a single-sheet workbook named after the file stem.
- Sheets larger than 100 rows render as a `head 10 + tail 5 + total count` summary so giant exports stay legible.
- Cells with `|` or embedded newlines are escaped to `\|` and `<br>` so the rendered GFM table never breaks.
- CSV decoding probes `utf-8-sig` first (handles BOM + plain UTF-8), then runs a `chardet` confidence-gated detection step (≥ 0.70 confidence) so non-Western encodings (Shift-JIS, GBK, ISO-8859-5, …) are decoded with the right codec. If neither succeeds, it falls back to `cp1252` (handles legacy Excel exports) and emits a `WARNING` log naming the file so the user can spot mojibake before it lands in the vault. You will not get a `UnicodeDecodeError`.
- Header detection is currently a heuristic: the first row is treated as headers when every cell is a non-empty string. A `has_header` override is tracked in [#165](https://github.com/Geoffe-Ga/Creek-Vault/issues/165).

## Presentation (`presentation`)

- `.pptx` files emit **one fragment per deck**. Each slide becomes a `## Slide N: Title` section with the body inlined and a `**Speaker notes:**` sub-section when the slide carries notes.
- The deck title comes from `core_properties.title` if set, otherwise the file stem.

## Images (`images`)

- Drives `pytesseract` against PNG/JPG/TIFF and via `pdf2image` for PDF pages.
- The OCR backend is **injectable**: swap in a different engine by implementing `creek.ingest.images.OcrEngine` and passing it to `ImageIngestor(backend=…)`. Useful for tests and for trying alternative OCR engines.
- Confidence below `OCRConfig.min_confidence` lands the fragment in the review queue.

## Google Drive

`creek gdrive` is a **read-only** Drive mirror. It uses OAuth, caches the refresh token at `GoogleDriveConfig.token_file` (`0o600`), and writes nothing back to Drive.

```bash
# First run opens a browser for OAuth; subsequent runs are non-interactive.
creek gdrive --download --staging ~/staging/gdrive

# The staging tree mirrors the Drive folder hierarchy. Run the
# matching ingestor for each file family (ARCH-001 — there is no
# ``--type gdrive``; passing it now prints a redirect message).
creek ingest --type document    --input ~/staging/gdrive --vault ~/Obsidian/Creek-Vault
creek ingest --type spreadsheet --input ~/staging/gdrive --vault ~/Obsidian/Creek-Vault
creek ingest --type markdown    --input ~/staging/gdrive --vault ~/Obsidian/Creek-Vault
```

Subsequent `--download` runs are **incremental** — unchanged files are skipped, deletions are mirrored as soft deletes (the staging file is removed; the vault fragment isn't, so you can choose whether to `creek purge source` it).

## Idempotency

Every ingestor produces deterministic fragment IDs (`SHA-256(source, timestamp, content)[:12]`), so re-running `creek ingest` against the same input only writes fragments whose content has actually changed. This is what lets `creek process` be safe to run on a cron.

## AI-chat attribution (per turn)

Ingesting a Claude or ChatGPT conversation emits **two attributed fragments per turn**: the human turn is the owner's voice (`source.author = self`, full `voice_weight`) and the AI turn is AI-authored (`source.author = ai`, `voice_weight = 0.0`). Because `Fragment.voice_proxy_eligible` excludes non-`self` authors, the assistant's prose can never train the voice proxy, while threading/linking survives — both turns share the conversation id, turn index, and timestamp.

### Migrating an existing vault

Vaults ingested before per-turn attribution have **merged** chat fragments that fused both turns into one `source.author=self` fragment. Re-split them with the opt-in, idempotent migration:

```bash
creek ingest --refresh-ai-chat --vault ~/Vault
```

It walks `01-Fragments/`, re-splits each merged Claude/ChatGPT fragment into a human (`self`) and a quarantined AI (`ai`, `voice_weight=0.0`) fragment, and removes the merged file. Running it twice is a no-op; a vault with no merged chat fragments is unchanged. Afterwards, `creek voice-authenticity` (see [generation](./generation.md#voice-fidelity-feat-040)) should report a near-zero AI-corpus leak.

## Writing a new ingestor

Implement the four-stage contract from `creek.ingest.base.Ingestor`:

```python
from creek.ingest.base import Ingestor, ParsedFragment, RawDocument

class MyIngestor(Ingestor):
    def discover(self, source_path):       ...   # walk the input
    def parse(self, raw):                  ...   # bytes -> [ParsedFragment]
    def convert_to_markdown(self, frag):   ...   # ParsedFragment -> str
    def generate_frontmatter(self, frag):  ...   # ParsedFragment -> dict
```

Register it in `creek.ingest.INGESTOR_REGISTRY` under a unique key, add a matching `SourcePlatform` enum value in `creek.models`, and the CLI / `creek process` will pick it up. Tests should inject a stub backend (Protocol-based) so the ingestor doesn't actually exercise the optional dependency in CI — see `creek.ingest.images.OcrEngine` and `creek.ingest.spreadsheets.SpreadsheetBackend` for the pattern.
