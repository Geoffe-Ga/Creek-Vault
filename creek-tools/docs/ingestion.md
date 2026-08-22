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

If `--type` is omitted, `creek process` resolves an owner per file — see
[How `creek process` picks an ingestor](#how-creek-process-picks-an-ingestor).

`gdrive` is **not** a `--type` (ARCH-001) — it is a downloader. Run `creek gdrive --download --staging <dir>` to mirror Drive files locally, then run `creek ingest --type document --input <dir>` (or `--type spreadsheet`, etc.) against the staged directory. See [Google Drive](#google-drive) below.

## How `creek process` picks an ingestor

`creek process` runs **every** registered ingestor over the source tree,
because several of them recognise their input by its structure or content
rather than its extension: a Discord export is a `messages/<channel>/`
directory shape, a ChatGPT export is a particular JSON envelope, a
Substack post is a `<postid>.<slug>.html` filename. No dispatch table can
express those.

It then **arbitrates**. Fragments are grouped by the source file they came
from, and only the highest-priority ingestor that actually produced output
for that file is kept. The order lives in
`creek.ingest.routing.CLAIM_PRIORITY`, which reads specific to general:

```
discord · chatgpt · claude · substack     ← recognise the file's internals
markdown · code · spreadsheet · presentation · image · document
generic                                    ← "nothing better claimed it"
```

Two consequences worth knowing:

* **One *ingestor* per file, not one *fragment* per file.** An ingestor
  that wins a file keeps everything it produced for it — one fragment per
  sheet of a workbook, one per module and function of a `.py` file, one
  per turn pair of a conversation.
* **The winner's rendering is the fragment.** A fragment's id hashes its
  source path, timestamp and content, so the arbitration decides the body
  text, the id, and the `YYYY-MM-DD-` prefix on the vault filename.

Before this behaviour existed (issue #1304) every claimant's output was
written, so a `.txt`, `.html`, `.csv`, `.py` or README-shaped `.md` file
produced two or three fragments per run.

### Upgrading a vault ingested before this change

Nothing is migrated automatically, and nothing is deleted. Here is exactly
what happens and what you may want to do.

Re-running `creek process` over the same source produces the winning
ingestor's fragment, which resolves to the id it already had, so it
de-duplicates against itself as usual. The **losing** ingestor's fragments
from earlier runs stay in the vault as strays: they have different ids
(the two ingestors disagree on both body and timestamp), so nothing
matches them, and now that the loser never runs against those files again
nothing will ever revisit them. They are ordinary fragment notes — indexed,
linkable, and indistinguishable from real ones apart from being a second
copy of a file you only have one of.

`creek process` now names the affected files for you:

```
Contested sources: 3 (more than one ingestor claimed these; one won)
  A vault ingested before this release may still hold the losing
  ingestor's fragments. Nothing is deleted automatically; inspect with
  creek purge source --source-path <path> --match exact --dry-run
  /home/me/notes/log.txt
  ...
```

The recommended sequence, per file, is to look before you touch anything:

```bash
# 1. See every fragment in the vault that came from this source file.
creek purge source --source-path /home/me/notes/log.txt --match exact --dry-run
```

If there is more than one, you have a stray. Two ways forward:

* **Leave it.** It is a duplicate note, not corruption. This is the right
  answer if you have hand-edited or hand-linked either copy — see the
  caveat below.
* **Purge and re-process.** `creek purge source --source-path <path>
  --match exact --yes` deletes *every* fragment from that source (the
  stray *and* the current one), then `creek process` recreates the
  current one. Note the caveat: purging scrubs references, replacing
  `[[wikilinks]]` to the deleted notes with `[purged]` across the vault,
  and re-processing does **not** restore them.

Two smaller changes ride along:

* `.txt` and `.html` files now get `DocumentIngestor`'s rendering rather
  than the generic fallback's, so their body — and therefore their id and
  filename — differs from what a pre-#1304 run wrote. `GenericIngestor`
  also stamped an `authored_at` in frontmatter that `DocumentIngestor`
  leaves unset.
* README, `CLAUDE.md` and ADR `.md` files go to `MarkdownIngestor` rather
  than `CodeIngestor`, which keeps their YAML frontmatter out of the body
  but drops the `artifact_type` label.

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

### Quarantined consent logs — do not delete these blind

If `00-Creek-Meta/Processing-Log/consent-log.json` is ever unparsable — a
torn write from a crash or a full disk, or bad bytes on disk — Creek moves
it aside to a sibling named

```
00-Creek-Meta/Processing-Log/consent-log.json.corrupt-<timestamp>-<random>
```

and starts a fresh log, emitting a `WARNING` that names both paths. **That
quarantined file may be the only surviving record of what you consented
to**, so it is preserved byte for byte and is never overwritten or removed
by Creek. Two quarantines in the same second get distinct names, and an
existing `.corrupt-*` file is never clobbered.

Treat one appearing as a signal, not as litter: open it, recover the grants
you recognise, and re-confirm them. Only delete it once you have. A vault
tidy-up script that sweeps unknown files out of `Processing-Log/` will
destroy audit history — exclude this pattern.

An unreadable log — bad permissions, or a directory sitting at the path —
is a different case and is *not* quarantined: the command refuses with a
non-zero exit rather than guessing. Creek will never report "no consent
recorded" because it failed to read the record.

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
- CSV decoding is delegated to [`creek/ingest/encoding.py`](../creek/ingest/encoding.py), which probes `utf-8-sig` first (handles BOM + plain UTF-8), then consults `chardet`. A `chardet` guess is accepted asymmetrically: a **single-byte** codec (ISO-8859-5, cp1251, …) only at ≥ 0.70 confidence, a **strict multi-byte CJK** codec (Shift-JIS, GBK, GB18030, EUC-KR, Big5, …) at any confidence provided the bytes actually decode under it. The asymmetry is the point — CJK codecs reject non-conforming bytes, so a clean decode is evidence, while single-byte codecs decode everything, so a clean decode is not. Real CJK CSVs routinely score 0.3–0.6 and used to be mangled by the fallback; that is [#1589](https://github.com/Geoffe-Ga/Creek-Vault/issues/1589).
- If no codec is identified, the fallback chain is `cp1252` then `latin-1`, in that order, and a `WARNING` naming the file and the rejected guess is logged so mojibake is spottable before it lands in the vault. `cp1252` goes first because it is what legacy Excel exports actually are; `latin-1` runs second only to catch the five values `cp1252` leaves undefined (`0x81 0x8d 0x8f 0x90 0x9d`), which used to abort a run with `UnicodeDecodeError` — that is [#1591](https://github.com/Geoffe-Ga/Creek-Vault/issues/1591).
- A CSV that is genuinely **binary** rather than text raises `UndecodableBytesError` before reaching any all-accepting codec. That is deliberate: `latin-1` maps all 256 byte values, so the alternative is a silent fragment of garbage. The file is recorded in `IngestResult.errors` and skipped, rather than written to the vault.
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

### Split messages are one turn

A turn is not a message. Sending a thought in two messages before the model replies is ordinary usage, and so is a model that answers in two. Both ingestors now merge each run of consecutive same-role messages into one turn, joined by a **blank line** — a bare newline would let an indented code block or a list sent as its own message be absorbed into the previous paragraph. Anything that is neither a human nor an assistant message (a system prompt, a tool result) is skipped and no longer breaks the turn around it.

Before [#1333](https://github.com/Geoffe-Ga/Creek-Vault/issues/1333) each run kept exactly one message and dropped the rest silently. The costly half was the human side: those fragments are `source.author = self`, so the voice fingerprint was trained on a filtered sample of how the operator actually writes.

**Recovering an affected vault.** Re-ingest the export:

```bash
creek ingest --type claude --input ~/exports/claude --vault ~/Vault
```

The Claude and ChatGPT sources are unledgered (only `markdown` is — see [Idempotency](#idempotency) above), so nothing records those turns as already processed and the re-ingest genuinely recovers the missing text. One thing to know before running it:

* Nothing is deleted or rewritten. The recovered turn has different content, so it hashes to a **new** fragment id and is written alongside the truncated fragment already in the vault, which stays exactly where it is. Expect near-duplicates for every affected turn and remove the short ones by hand.

Turn *numbering* is unaffected: a run collapses into the same single turn it always produced, so `turn_index` and the `(turn N)` titles do not shift. The one exception is a ChatGPT conversation with a system or tool node between a question and its answer, which used to yield no fragments at all and now yields the turn it should always have.

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
