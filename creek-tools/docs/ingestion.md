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

## Symlinks in a source tree

Ingestion refuses a source tree that links out of itself. If any entry under
`--input` is a symlink whose target resolves outside that tree — or if
`--input` is itself such a link — the run stops with exit 1 and writes
nothing:

```
Symlink containment: Refusing to ingest /Users/me/exports: /Users/me/exports/notes/link.md
is a symlink whose target resolves outside that tree, so ingestion would write content
from outside the source into the vault. Remove or re-point the link, or ingest the
target's own directory directly.
```

This applies to every `--type`, to `creek process`, and to the
`creek.ingest` MCP tool, and it holds regardless of `redaction.enabled` —
containment is not part of the PII scan. There is no override flag; the fix
is to remove the link, re-point it inside the tree, or name the target's own
directory as `--input`.

On both `creek ingest` and `creek process` the refusal lands *before* the
first-time consent prompt, not after it. That ordering is the point: the
prompt's `Found: N file(s), X MB` / `Sample: ...` summary is built by walking
the source tree and `stat()`ing every entry, which follows an escaping link.
Refusing first means no out-of-tree filename is ever printed, and — under
`--yes`, which records consent immediately — no file count measured through
a link is ever written into
`00-Creek-Meta/Processing-Log/consent-log.json`, where it would persist and
suppress the prompt on every later run.

Symlinks that stay **inside** the source tree are fine and keep working, so
an ordinary alias (`latest -> 2026-08-01`, or `alias.md -> real.md`) needs no
change. A tree reached *through* a symlink is also fine — only links that
leave the named root are refused.

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

## Generic (`generic`)

- Fallback for any file whose extension no other ingestor claims (`.txt`, `.log`, plain text, and anything not in the specialized ingestors' extension sets). Binary files (detected via a null-byte / control-character heuristic) and empty or whitespace-only files are skipped — no fragment is written.
- Routes to `01-Fragments/Unsorted/` with `source.platform: other` (`SourcePlatform.OTHER`).
- The fragment id is derived from the file's **mtime**, not the wall-clock time you ran `creek ingest` — re-ingesting an unchanged file reuses the same mtime and therefore the same id, so the write is a genuine no-op. (Wall clock is used only as a fallback for a pathless/synthetic document where `stat()` fails.)
- `created` and `authored_at` in the frontmatter are both the file's mtime, kept in UTC.
- Known limitation: this source has no ingest ledger (see [Idempotency](#idempotency)), so a bare `touch` — or any edit — bumps the mtime and mints a *new* fragment rather than updating the existing one. Tracked in [#953](https://github.com/Geoffe-Ga/Creek-Vault/issues/953).

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

Fragment IDs are deterministic — `SHA-256(source, timestamp, content)[:12]` — so identity is stable only if `timestamp` is. Every ingestor derives `timestamp` from the source itself rather than from wall clock: the message's own epoch for conversation exports (Claude/ChatGPT/Discord), and the file's own metadata (frontmatter date, creation time, or modification time, depending on the ingestor) for file-based sources. Re-running `creek ingest` against unchanged input reuses the same ids and writes nothing new, which is what lets `creek process` be safe to run on a cron.

The caveat: a source keyed on mtime treats *any* filesystem touch as a change, not just a meaningful edit — see [Generic](#generic-generic) for the concrete case. Ledger-backed sources avoid this: the markdown (journal) source tracks a stable `source_key` per file in a per-source ledger and, on a changed mtime, **updates the existing fragment in place** (preserving its id, classifications, and links) instead of minting a duplicate — see [`docs/idempotent-ingest.md`](idempotent-ingest.md). `generic` doesn't have a ledger yet, so for it, mtime-keyed identity only buys "unchanged file re-ingests as a no-op," not "edited file updates in place" ([#953](https://github.com/Geoffe-Ga/Creek-Vault/issues/953)).

## AI-chat attribution (per turn)

Ingesting a Claude or ChatGPT conversation emits **two attributed fragments per turn**: the human turn is the owner's voice (`source.author = self`, full `voice_weight`) and the AI turn is AI-authored (`source.author = ai`, `voice_weight = 0.0`). Because `Fragment.voice_proxy_eligible` excludes non-`self` authors, the assistant's prose can never train the voice proxy, while threading/linking survives — both turns share the conversation id, turn index, and timestamp. That guarantee is *enforced* rather than aspirational as of [#1213](https://github.com/Geoffe-Ga/Creek-Vault/issues/1213): the voice corpus refuses any fragment whose `source.author` is not `self` in `creek.generate.voice._eligible_register`, so it holds for `other`- and `collaborative`-authored content too — including a document whose file metadata named someone else — not only for the AI turns that also carry `voice_weight = 0.0`.

### Migrating an existing vault

Vaults ingested before per-turn attribution have **merged** chat fragments that fused both turns into one `source.author=self` fragment. Re-split them with the opt-in, idempotent migration:

```bash
creek ingest --refresh-ai-chat --vault ~/Vault
```

It walks `01-Fragments/`, re-splits each merged Claude/ChatGPT fragment into a human (`self`) and a quarantined AI (`ai`, `voice_weight=0.0`) fragment, and removes the merged file. Running it twice is a no-op; a vault with no merged chat fragments is unchanged. Afterwards, `creek voice-authenticity` (see [generation](./generation.md#voice-fidelity-feat-040)) should report a near-zero AI-corpus leak.

## Document attribution (the name on the file)

A DOCX carries `core_properties.author` and a PDF carries `/Author` — Word stamps one on every save. That name answers "what name is on the file", which is **not** the question `source.author` answers ("whose views does this stand for?", on the `self|ai|other|collaborative` axis). Ingest keeps the two apart:

- `source.author` gets `other` whenever a document carries a non-empty author name. Same rule the cleaning pass already applies to an explicit author name, and it fails closed: `self` is what unlocks the `intimate` privacy tier and voice/skill generation, so guessing `self` for a document that came from someone else would feed both from material that isn't yours. Guessing `other` for your own document only under-uses it, and the name below is what lets you correct it. A document with **no** author (or a blank one) keeps the `self` default — absence of a name is not evidence about anyone.
- `source.author_name` gets the extracted name verbatim. It is inert: nothing routes, classifies, or weights voice on it. It exists so the name is queryable rather than discarded.
- `source.author_slug` is **not** set from an extracted name. That field names an `11-Other-Authors/<slug>/` folder, and setting it would relocate the fragment out of `01-Fragments/` and zero its `voice_weight` from the folder manifest — too consequential to infer from a "Created by" field. Promote an `author_name` to a real `author_slug` deliberately, when you actually mean it.

RTF and HTML never populate an author at all: `DocumentIngestor` reads embedded metadata only for `.docx` and `.pdf`, so an RTF `\author` group and an HTML `<meta name="author">` are ignored and those fragments keep the `self` default.

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
