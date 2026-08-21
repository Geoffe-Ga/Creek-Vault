# Seeding a vault

**Seeding** is getting your own material — notes, chat exports, documents,
spreadsheets, Drive files — into a Creek vault as fragments. Everything else
Creek does (classification, linking, voice) reads what seeding wrote, so a
vault seeded from one source sounds like one source.

There are two ways in, and they are not equivalent:

- **The CLI** — `creek ingest` on the machine that holds the files. Full
  coverage, all eleven ingestors, directory-shaped exports, Google Drive.
- **The network** — the `/v1` HTTP API and the MCP tool surface, for an
  application seeding a vault on a user's behalf (this is what
  [Adepthood](#the-adepthood-half) uses). Narrower: one document per
  request, plus export archives.

Everything on this page was run against a scratch vault, and every quoted
output is the output that command actually produced. Where something could
**not** be executed here it is marked
[Not verified](#what-was-not-verified-here) rather than described
confidently.

## Contents

- [Before you start](#before-you-start)
- [Which shape is my source?](#which-shape-is-my-source)
- [Seeding from the CLI](#seeding-from-the-cli)
- [Seeding over the network](#seeding-over-the-network)
  - [Ingests, but does not classify or link](#seeding-over-the-network-ingests-but-does-not-classify-or-link)
- [Google Drive](#google-drive)
- [Four traps that silently cost you fragments](#four-traps-that-silently-cost-you-fragments)
- [Privacy of seeded content](#privacy-of-seeded-content)
- [What was not verified here](#what-was-not-verified-here)
- [Related reading](#related-reading)

## Before you start

Make a vault. `creek init` writes the folder scaffold, the starter
`creek_config.yaml`, and the skill tree:

```bash
creek init --vault ~/Creek-Vault
```

It ends by telling you to look at the config before doing anything else, and
it means it:

```
Edit <vault>/00-Creek-Meta/creek_config.yaml before running creek process — the
defaults are intentionally cautious.
```

Then seed one source at a time with `creek ingest`, checking what landed
before moving to the next. `creek process` chains every ingestor in one pass,
but while you are learning the shapes, one at a time is easier to read.

## Which shape is my source?

The single most common seeding mistake is sending the wrong *shape*. Creek
cares about four:

| Shape | What it means | Example |
|-------|---------------|---------|
| **Single file** | One document, one path. | A `.md` note, a `.csv`, a `.pdf`. |
| **Directory** | A folder Creek walks. | A folder of Obsidian notes; a source tree. |
| **Export archive** | The `.zip` a platform hands you, holding many conversations in a fixed internal layout. | ChatGPT, Claude, Discord, Substack exports. |
| **OAuth connector** | A remote account Creek reads with your permission. | Google Drive. |

Here is every source, its shape, and how to seed it:

| Source | Shape | CLI | Over the network |
|--------|-------|-----|------------------|
| Obsidian / loose markdown | Directory (a single `.md` also works) | `creek ingest --type markdown` | `POST /v1/uploads`, one `.md` per call |
| Plain text / unclaimed extensions | Directory | `creek ingest --type generic` | `POST /v1/uploads` (`.txt` routes to `document`) |
| Documents (`.docx`, `.pdf`, `.html`, `.rtf`) | Directory of files | `creek ingest --type document` | `POST /v1/uploads`, one file per call |
| Spreadsheets (`.xlsx`, `.csv`) | Directory of files | `creek ingest --type spreadsheet` | `POST /v1/uploads`, one file per call |
| Presentations (`.pptx`) | Directory of files | `creek ingest --type presentation` | `POST /v1/uploads`, one file per call |
| Images / OCR | Directory of images | `creek ingest --type image` | `POST /v1/uploads`, one image per call |
| Source code and ADRs | Directory tree | `creek ingest --type code` | `POST /v1/uploads` — **but see [the fidelity trap](#4-the-same-py-file-becomes-different-fragments-by-cli-and-by-upload)** |
| ChatGPT history | Export archive | `creek ingest --type chatgpt` on the **unpacked** folder | `POST /v1/uploads` with the `.zip` |
| Claude history | Export archive | `creek ingest --type claude` on the **unpacked** folder | `POST /v1/uploads` with the `.zip` |
| Discord history | Export archive | `creek ingest --type discord` on the **unpacked** folder | `POST /v1/uploads` with the `.zip` |
| Substack archive | Export archive | `creek ingest --type substack` on the **unpacked** folder | `POST /v1/uploads` with the `.zip` |
| Google Drive | OAuth connector | `creek gdrive --download`, then ingest the staging folder | Status / sync / disconnect only — **you cannot finish connecting over the network yet ([#1568](https://github.com/Geoffe-Ga/Creek-Vault/issues/1568))** |

These are the eleven `--type` values `creek ingest` accepts. There is no
twelfth, and there is no `--type gdrive`:

<!-- capability-set: ingest-types -->

| `--type` | Seeds |
|----------|-------|
| `chatgpt` | ChatGPT conversation export (unpacked) |
| `claude` | Claude conversation export (unpacked) |
| `code` | A source tree, decomposed into module and function fragments |
| `discord` | Discord export (unpacked), `messages/<channel-id>/` layout |
| `document` | `.docx`, `.pdf`, `.html`, `.htm`, `.txt`, `.rtf` |
| `generic` | Plain-text fallback for extensions nothing else claims |
| `image` | Images, via OCR |
| `markdown` | `.md` / `.markdown` notes |
| `presentation` | `.pptx` decks |
| `spreadsheet` | `.xlsx` and `.csv` |
| `substack` | A Substack export (unpacked) |

<!-- /capability-set -->

Ask for anything else and you are told so, with exit status `2`:

```console
$ creek ingest --type bogus --input ./src --vault ~/Creek-Vault --yes
Unknown ingestor type 'bogus'. Known types: chatgpt, claude, code, discord,
document, generic, image, markdown, presentation, spreadsheet, substack
```

### The `--type` you pass is not the `platform` you get

Every fragment Creek writes carries a `source.platform` in its frontmatter.
There are fourteen platforms and eleven `--type` values, the two lists share
most of their names, and they do **not** line up. Here is the whole mapping:

<!-- capability-set: source-platforms -->

| `source.platform` | Stamped by |
|-------------------|------------|
| `chatgpt` | `chatgpt` |
| `claude` | `claude` |
| `code` | `code`, `markdown` |
| `discord` | `discord` |
| `document` | `document` |
| `email` | *nothing — no ingestor produces it* |
| `essay` | `markdown` |
| `image_ocr` | `image` |
| `journal` | `markdown` |
| `markdown` | `markdown` |
| `other` | `generic`, `document` |
| `presentation` | `presentation` |
| `spreadsheet` | `spreadsheet` |
| `substack` | `substack` |

<!-- /capability-set -->

**Every producer cell above is executed, not read off a source literal.**
`tests/test_seeding_docs_capability_set.py` builds a fixture for each of
the eleven ingestors, runs it, and compares the `source.platform` values it
actually stamps against this table — so a platform changed in code fails
the build even when every name in the table still matches. Nine ingestors
run on their real backend. Two are driven through their own documented
injection points because their defaults cannot run unattended: `image`
takes a stub `OcrEngine` (the real one shells out to a `tesseract` system
binary) and `presentation` takes a stub `PresentationBackend` (there is no
library-free way to author a `.pptx` fixture). The code under test — the
ingestor's own `generate_frontmatter` — is the real thing in both cases.

Three rows in that table are worth knowing about *before* you seed, because
each one puts a fragment somewhere you did not ask for:

- **`--type markdown` decides `journal` / `essay` / `code` for you, from the
  folder name.** A file under `daily/`, `journal/` or `diary/` is stamped
  `platform: journal`; one under a folder starting `essay` or under
  `writing/` is stamped `platform: essay`; a note whose body reads as
  technical prose is stamped `platform: code`. Nothing you pass on the
  command line overrides this. Verified in one run over a parent folder:

  ```console
  $ creek ingest --type markdown --input ./src --vault ~/Creek-Vault --yes
  Ingest summary: 2 created, 0 updated, 0 tombed, 0 skipped

  $ grep -r 'platform:' ~/Creek-Vault/01-Fragments
  01-Fragments/Journal/2026-08-21-Morning.md:  platform: journal
  01-Fragments/Notes/2026-08-21-Plain-note.md:  platform: markdown
  ```

  The platform picks the vault subfolder — `Journal/`, `Writing/`,
  `Notes/` — and feeds audience scoring, so a note filed under `writing/`
  for unrelated reasons is treated as public-facing prose from then on.

- **`--type document` splits its own inputs in two.** `.docx` and `.rtf`
  are stamped `platform: document` and filed under `Documents/`; `.txt`,
  `.html` and `.htm` are stamped `platform: other` and land in
  `Unsorted/` — the same place `--type generic` puts things. One run over
  a folder holding all four:

  ```console
  $ creek ingest --type document --input ./src/docs --vault ~/Creek-Vault --yes --strict
  Found: 4 file(s), 0.0 MB.
  Sample: memo.rtf, page.html, plain.txt, report.docx
  Ingest summary: 4 created, 0 updated, 0 tombed, 0 skipped
  Ingested 4 fragment(s).

  $ grep -r 'platform:' ~/Creek-Vault/01-Fragments
  01-Fragments/Unsorted/2026-08-21-page.md:  platform: other
  01-Fragments/Unsorted/2026-08-21-plain.md:  platform: other
  01-Fragments/Documents/2013-12-23-report.md:  platform: document
  01-Fragments/Documents/2026-08-21-memo.md:  platform: document
  ```

  (`report.docx` files under 2013 because the fragment's date comes from
  the file's own DOCX core properties, not from when you ingested it.)

  The `--type` is accepted and the fragment is created either way; only
  the platform and the filing differ. `.pdf` takes the same branch as
  `.docx` and `.rtf`, but no PDF was run here — see
  [what was not verified](#what-was-not-verified-here).

- **`email` has no producer at all.** Outside the test suite, exactly two
  places reference `SourcePlatform.EMAIL` — the audience scorer
  (`creek/classify/audience.py`) and the vault writer
  (`creek/vault/writer.py`, which files it under `Messages/`) — and
  nothing in `creek/ingest/` emits it. The **redactor does not know the
  platform at all**: `grep -rn SourcePlatform creek/redact/` returns zero
  hits. (`creek/redact/patterns.py` does have an `email` *pattern*, but
  that matches addresses inside a body and is unrelated to
  `source.platform`.) There is no email ingestor and no `--type email`. A
  fragment only gets that platform if something outside the ingest
  package sets it.

The `creek.journal` MCP tool and its HTTP twin `PUT
/v1/journal-entries/{external_id}` also write fragments — see
[over the network](#seeding-over-the-network) for the route and
[mcp.md](./mcp.md) for the tool's gates.

## Seeding from the CLI

Every command below takes `--vault <path>` and `--yes` (which auto-grants the
ingest consent prompt and records the grant in the consent log). Add
`--strict` while you are setting up: it turns "I found your files but made
nothing out of them" into a non-zero exit instead of a warning you might
scroll past.

### Markdown notes — a directory, or one file

```console
$ creek ingest --type markdown --input ./src/md --vault ~/Creek-Vault --yes
First time processing /…/src/md.
Found: 1 file(s), 0.0 MB.
Sample: note.md
Consent auto-granted via --yes; recorded in consent log.
Ingest summary: 1 created, 0 updated, 0 tombed, 0 skipped
Ingested 1 fragment(s).
```

The fragment lands at
`<vault>/01-Fragments/Notes/2026-08-21-Morning-Note.md`.

A single file path works too — see
[trap 2](#2-a-single-file-is-reported-as-found-0-files) for the confusing
thing it prints while doing so.

### Spreadsheets, documents, presentations, images

Same shape, different `--type`. A `.csv` lands under `01-Fragments/Data/`; a
`.py` tree under `01-Fragments/Technical/`:

```console
$ creek ingest --type code --input ./src/code --vault ~/Creek-Vault --yes --strict
Consent auto-granted via --yes; recorded in consent log.
Ingest summary: 1 created, 0 updated, 0 tombed, 0 skipped
Ingested 1 fragment(s).
```

That run wrote `01-Fragments/Technical/2026-08-21-function-braid.md` — a
*function-level* fragment, not the whole file as one blob. That difference
matters when you compare it to uploading the same file
([trap 4](#4-the-same-py-file-becomes-different-fragments-by-cli-and-by-upload)).

A real `.docx` and a real `.pptx` were run (see the `--type document`
console block above, and `talk.pptx` landing as
`01-Fragments/Decks/2026-08-21-talk.md` with `platform: presentation`).
`.pdf`, `.xlsx` and real OCR were
[not exercised here](#what-was-not-verified-here).

### Chat exports — unpack the `.zip` first

The CLI ingestors for ChatGPT, Claude, Discord and Substack read a
**directory**. Unpack the archive the platform gave you and point `--input`
at the unpacked folder.

```console
$ creek ingest --type claude --input ./src/claude --vault ~/Creek-Vault --yes --strict
Ingest summary: 2 created, 0 updated, 0 tombed, 0 skipped
Ingested 2 fragment(s).

$ creek ingest --type chatgpt --input ./src/chatgpt --vault ~/Creek-Vault --yes --strict
Ingest summary: 2 created, 0 updated, 0 tombed, 0 skipped
Ingested 2 fragment(s).

$ creek ingest --type discord --input ./src/discord --vault ~/Creek-Vault --yes --strict
Ingest summary: 1 created, 0 updated, 0 tombed, 0 skipped
Ingested 1 fragment(s).
```

Each export has an internal layout the ingestor insists on, and giving it the
wrong one is quiet — that is
[trap 1](#1-a-source-can-produce-zero-fragments-and-still-exit-0). Verified
here:

- **Claude** wants a top-level *object* with a `conversations` key (or a
  single-conversation object with `conversation_id` and `messages`). A bare
  JSON *list* discovers nothing.
- **ChatGPT** wants each conversation's `mapping` of message nodes wired
  together by `parent` / `children`.
- **Discord** wants `<root>/messages/<channel-id>/messages.json` alongside
  `channel.json`.
- **Substack** rglobs `*.html` and **skips any file whose name does not start
  with the post id** — `on-silt.html` is ignored, `164523.on-silt.html` is
  ingested.

## Seeding over the network

The `/v1` API is how an application seeds a vault it does not share a
filesystem with. It publishes exactly nine routes:

<!-- capability-set: v1-routes -->

| Method | Path |
|--------|------|
| `GET` | `/v1/capabilities` |
| `PUT` | `/v1/journal-entries/{external_id}` |
| `POST` | `/v1/reflections` |
| `GET` | `/v1/wheel` |
| `POST` | `/v1/uploads` |
| `GET` | `/v1/connectors/drive` |
| `POST` | `/v1/connectors/drive/syncs` |
| `DELETE` | `/v1/connectors/drive` |
| `GET` | `/v1/health` |

<!-- /capability-set -->

Two of those nine were observed writing a fragment into `01-Fragments/`
here — `POST /v1/uploads` and `PUT /v1/journal-entries/{external_id}`:

```console
PUT /v1/journal-entries/adepthood:j:1  200 {"status":"ok","fragment_id":"frag-22d4fe656bcb",
                                            "action":"created","tier":"open"}
# -> 01-Fragments/Journal/2026-08-21-adepthood-j-1-f326876331f3.md

POST /v1/uploads                        200 {"status":"ok","fragment_id":"frag-a0c1d280304d",
                                            "action":"created","source_type":"markdown"}
# -> 01-Fragments/Notes/2026-08-21-Hi.md
```

The Drive connector is the third seeding surface, but its sync route could
only be observed refusing here — see [Google Drive](#google-drive). The
remaining six wrote no fragment in these runs; note that `POST
/v1/reflections` was only reachable as a `503 temporarily_unavailable` (no
LLM backend was configured), so "it does not seed" is observed for its
refusal path only.

The rest of this section is about `POST /v1/uploads`, which is the route an
application uses to seed arbitrary documents. The journal route is
narrower — one typed entry, addressed by your own external id — and its
gates are documented in [mcp.md](./mcp.md).

### Authentication is not optional

There is no anonymous access, and the server refuses to *start* without
consumer tokens configured:

```
ValueError: CREEK_MCP_CONSUMER_TOKENS is not set (consumer=token pairs).
/v1 has no anonymous access, so it refuses to serve without authentication
configured.
```

A token shorter than 32 characters is refused at startup too, with the
recipe for making a good one:

```
ValueError: consumer 'demo' token is 26 chars, below the 32-char minimum;
rotate it with python -c "import secrets; print(secrets.token_urlsafe(32))"
```

An unauthenticated request gets `401`:

```json
{"code": "unauthenticated", "message": "the request carried no valid consumer credential"}
```

Full setup, rotation and header reference: [api.md](./api.md).

### `POST /v1/uploads` — one document, or one export archive

Send the file base64-encoded, with an `external_id` (your idempotency key)
and a **required** `tier`:

```json
{
  "filename": "note.md",
  "content_base64": "…",
  "external_id": "up-md-doc-2",
  "tier": "personal"
}
```

A success names the ingestor that claimed it:

```json
{"status": "ok", "tier_ceiling": "personal", "external_id": "up-md-doc-2",
 "fragment_id": "frag-69f04c7006e9", "action": "created", "source_type": "markdown"}
```

**An export `.zip` is accepted here**, and its type is detected from what is
inside the archive — you do not declare it. A zip holding
`messages/1234/messages.json` came back as Discord:

```json
{"status": "ok", "external_id": "up-zip-doc-1", "fragment_id": "frag-0f03d4fed20b",
 "action": "created", "source_type": "discord"}
```

The cap is **10 MiB** (`MAX_UPLOAD_BYTES`), checked on the encoded length
first and then the decoded length, before anything is written. It is a code
constant, not a setting.

Which extensions are accepted, and what each becomes:

<!-- capability-set: upload-extensions -->

| Extension | Result |
|-----------|--------|
| `.md`, `.markdown` | `markdown` |
| `.docx`, `.pdf`, `.html`, `.htm`, `.txt`, `.rtf` | `document` |
| `.xlsx`, `.csv` | `spreadsheet` |
| `.pptx` | `presentation` |
| `.png`, `.jpg`, `.jpeg`, `.gif`, `.bmp`, `.tiff`, `.webp` | `image` |
| `.zip` | `archive` |
| `.json`, `.jsonl`, `.ndjson` | `refused` |
| `.tar`, `.tgz`, `.gz`, `.bz2`, `.xz`, `.7z`, `.rar` | `refused` |
| `.doc`, `.xls`, `.ppt` | `refused` |

<!-- /capability-set -->

A refusal is a `415`, deliberately — the alternative was filing your whole
conversation history as one undifferentiated blob:

```json
{"code": "unsupported_source",
 "message": "this file format cannot be ingested as one document: unpack a conversation export or archive and run `creek ingest --type <chatgpt|claude|discord|substack> --input <dir>`, or re-save a legacy Office document as .docx, .xlsx or .pptx"}
```

Note that `.zip` appears in the routing table as a refusal *and* is accepted
in practice: the archive fork runs **before** extension routing, so a `.zip`
never reaches the refusal. `.tar` and the rest do reach it — repack as `.zip`.

### Tier is checked before anything is read

`tier` is required on every upload, is never defaulted, and is compared
against the ceiling on the request **before a single byte is decoded**. At
the default `open` ceiling, a `personal` upload is refused outright:

```json
{"code": "privacy_refused", "message": "resolved content exceeds the declared tier ceiling"}
```

Raise the ceiling with the `X-Creek-Tier-Ceiling: personal` header and the
same upload succeeds. `intimate` is unreachable over `/v1` at all — it fails
the published schema:

```json
{"code": "invalid_request", "message": "the request does not satisfy the published schema"}
```

`GET /v1/capabilities` says the same thing up front:

```json
{"tier_model": {"ceilings": ["open", "personal"], "default": "open",
                "intimate_never_egresses": true},
 "capabilities": ["capabilities", "journal-upsert", "reflections", "wheel",
                  "upload", "drive-connector"]}
```

The MCP tool `creek.upload` is the same surface over stdio, and its gates are
documented row-by-row in [mcp.md](./mcp.md).

### Seeding over the network ingests, but does not classify or link

**This is the most important limitation on this page.** Everything above gets
your bytes into the vault as fragments. It does not make them *usable*.

`/v1` publishes nine routes across six capabilities, and **none of them
classifies or links**:

```console
$ grep -ic "classify\|link" creek_mcp/api/routes.py
0
```

`POST /v1/uploads` calls `run_ingest` and stops there. `creek.classify` and
`creek.link` exist only as **MCP tools** — there is no HTTP route for either.

So a vault seeded entirely over the network contains fragments with:

- no APTITUDE frequency,
- no Archetypal Wavelength phase,
- no resonances, and
- `privacy_tier: unclassified` (see
  [Privacy of seeded content](#privacy-of-seeded-content) — `unclassified`
  ranks *with* `personal`, so a fresh vault is fail-closed rather than open).

Nothing errors. The upload returns `200`, the fragments are genuinely on disk,
and the vault is simply **inert** — the Higher Self Resonance runs over an
unclassified corpus.

**Until this closes, a network-only seeding flow is incomplete.** Someone with
shell access on the vault host has to run:

```console
$ creek classify --vault <vault>
$ creek link --vault <vault>
```

Tracked as
[#1570](https://github.com/Geoffe-Ga/Creek-Vault/issues/1570). Unlike the Drive
OAuth gap ([#1568](https://github.com/Geoffe-Ga/Creek-Vault/issues/1568)),
which was a deliberate cut, this one was not filed until the seeding epic's
Definition of Done was re-read against the shipped routes — the DoD promises
fragments land *"correctly-typed, correctly-tiered — over the network, with no
CLI and no shell access,"* and typing and tiering are exactly what `classify`
and `link` produce.

## Google Drive

Drive is an **OAuth connector**, not an ingestor. `creek ingest --type
gdrive` refuses and tells you what to do instead (exit status `2`):

```console
$ creek ingest --type gdrive --input ./src/md --vault ~/Creek-Vault --yes
gdrive is a downloader, not an ingestor. Run creek gdrive --download --staging
<dir> to mirror Drive files locally, then point the appropriate ingestor at the
staging directory (e.g. creek ingest --type document --input <dir> for .docx /
.pdf files).
```

Before authorising anything, `creek gdrive --check` is a read-only doctor
that makes no network calls at all:

```console
$ creek gdrive --check --staging ./gdrive-staging
Credentials (credentials.json): MISSING
Token (token.json): absent — run `creek gdrive --download` once to authorise
Drive reachable: skipped (no token)

No network calls were made. Nothing was downloaded.
```

`creek gdrive` has no `--vault` flag, so point it at your vault's config with
`CREEK_CONFIG` or `--config` — otherwise it runs on built-in defaults and
says so.

### What works over the network, and what does not

`#1527` put the Drive connector's **state, sync and disconnect** on `/v1`.
All three respond:

```console
GET  /v1/connectors/drive        200 {"status":"ok","connection":"not_connected",
                                      "scopes":["https://www.googleapis.com/auth/drive.readonly"],
                                      "can_sync":false}
POST /v1/connectors/drive/syncs  503 {"code":"unavailable",
                                      "message":"the vault is not available to serve this request"}
DELETE /v1/connectors/drive      200 {"status":"ok","connection":"not_connected",
                                      "remote_revoked":false}
```

**A user cannot complete a Drive connection over the network today.** There
is no route that begins the OAuth grant — the first authorisation is still
`creek gdrive --download` on the host machine, because the installed-app
loopback flow needs a browser on the machine holding the client secret. That
is tracked as
[#1568](https://github.com/Geoffe-Ga/Creek-Vault/issues/1568) and was
deliberately cut from #1527, not overlooked. Until it lands, `sync` against a
disconnected connector refuses (the `503` above); the remedy is a CLI command
the wire message does not name.

Anything about Drive beyond those three responses — a real grant, a token
exchange, an actual sync that ingests files — is
[not verified here](#what-was-not-verified-here).

## Four traps that silently cost you fragments

### 1. A source can produce zero fragments and still exit 0

`--strict` catches *some* of this and not all of it. It fires when the
ingestor discovered inputs but produced nothing:

```console
$ creek ingest --type claude --input ./src/claude-badshape --vault ~/Creek-Vault --yes --strict
WARNING: discovered 1 claude input(s) but produced 0 fragments — the export
format may be unrecognized.
# exit status 1
```

It stays silent when the ingestor discovered **nothing at all**, even though
the consent preflight counted your files with a different scanner:

```console
$ creek ingest --type substack --input ./src/sub --vault ~/Creek-Vault --yes --strict
Found: 1 file(s), 0.0 MB.
Sample: on-silt.html
Ingest summary: 0 created, 0 updated, 0 tombed, 0 skipped
Ingested 0 fragment(s).
# exit status 0
```

"Found: 1 file(s)" and "Ingested 0 fragment(s)" in the same run means the
file was skipped by the ingestor's own discovery rule — here, the missing
Substack post-id prefix. **Read the `Ingested N fragment(s)` line, not the
`Found` line.**

### 2. A single file is reported as `Found: 0 file(s)`

Passing one file directly works, but the preflight counter only walks
directories, so it tells you nothing was found while a fragment is in fact
about to be written:

```console
$ creek ingest --type markdown --input ./src/solo/solo.md --vault ~/Creek-Vault --yes
Found: 0 file(s), 0.0 MB.
Consent auto-granted via --yes; recorded in consent log.
Ingest summary: 1 created, 0 updated, 0 tombed, 0 skipped
Ingested 1 fragment(s).
```

Without `--yes` you would be answering a consent prompt that understates what
is about to happen.

### 3. Uploading a raw `.json` export is refused, and that is the feature

`conversations.json` is many conversations, not one document. It is refused
with a `415` rather than filed as a blob. Send the platform's `.zip`, or
unpack it and use the CLI.

### 4. The same `.py` file becomes different fragments by CLI and by upload

`creek ingest --type code` decomposes a source file into module- and
function-level fragments. `.py` is not in the upload routing table, so the
same bytes uploaded fall through to `generic` and land as **one
undecomposed fragment**. For code, prefer the CLI.

## Privacy of seeded content

This section states what the writer actually did, checked by reading the
bytes it wrote — not what the pipeline intends.

### A CLI-seeded fragment is `unclassified`, and its body is in cleartext

`creek ingest` has **no tier or privacy flag at all** — grepping its `--help`
for `tier` or `privacy` returns nothing. Every fragment it writes is stamped
`privacy_tier: unclassified`, with the source text verbatim after the
frontmatter. This is `01-Fragments/Notes/2026-08-21-Morning-Note.md` as it
sits on disk after the markdown run above:

```yaml
id: frag-2b29e5ee28f8
privacy_tier: unclassified
source:
  origin_key: note.md
  original_file: /…/src/md/note.md
  platform: markdown
title: Morning Note
---

The creek runs low in August. I keep coming back to silt.
```

Three things to take from that:

1. **The tier is `unclassified`, not `open` and not `personal`.** Nothing has
   read your content yet.
2. **The body is not encrypted and not redacted.** It is the file's prose,
   in the clear, inside the vault.
3. **`source.original_file` records the absolute path of your original
   file** — which leaks your directory structure and username to anyone who
   later reads the fragment.

### `unclassified` is not `open` — a fresh vault is fail-closed

The reassuring half, also checked by executing the ranking rather than
reading intent. `unclassified` ranks **with `personal`**, not with `open`:

```python
{PrivacyTier.OPEN: 0, PrivacyTier.UNCLASSIFIED: 1,
 PrivacyTier.PERSONAL: 1, PrivacyTier.INTIMATE: 2}
```

and admission at each ceiling:

```
ceiling open      -> unclassified admitted: False
ceiling personal  -> unclassified admitted: True
ceiling intimate  -> unclassified admitted: True
ceiling all       -> unclassified admitted: True
```

So a freshly seeded vault is **fail-closed against the default `open`
ceiling**: a reader over MCP or `/v1` at the default ceiling sees none of it.
Seeding is safe in the sense that nothing egresses. It is *not* safe in the
sense of being classified or encrypted at rest. Run `creek classify` before
treating any tier as meaningful — see
[classification.md](./classification.md).

One sharp edge worth knowing: `unclassified` carries **two opposite
orderings** in the codebase. In the read-ceiling ranking above it means
"handle carefully" and ranks high; in the escalate-only privacy merge it
means "no claim was made" and ranks lowest. Do not assume one from the other.

`privacy_tier` is a **one-way ratchet** — it only ever escalates. "Reset
everything to the most restrictive tier" is not a cleanup step you can undo;
it is permanent.

### An uploaded fragment carries the tier you declared

The upload surface behaves oppositely, and better. `tier` is mandatory,
refused rather than defaulted when omitted, and checked before decoding. The
declared tier is genuinely stamped on disk — after the runs above:

```
01-Fragments/Notes/2026-08-21-hi.md:privacy_tier: personal
01-Fragments/Data/2026-08-21-up-csv-doc-2-….md:privacy_tier: open
01-Fragments/Messages/2026-08-01-silt-….md:privacy_tier: open
```

### Where the bytes physically land

| What | Where | Redacted? |
|------|-------|-----------|
| The fragment | `<vault>/01-Fragments/<folder>/<date>-<title>.md` | No — body verbatim |
| Staged upload bytes | `<vault>/00-Creek-Meta/adepthood/uploads/` | **No** — the original file sits there in the clear until purged ([#1228](https://github.com/Geoffe-Ga/Creek-Vault/issues/1228)) |
| Unpacked archive contents | `<vault>/00-Creek-Meta/adepthood/archive-unpack/` | Cleaned up — verified **empty** after a `.zip` upload |

**Only two directories are staging roots, and `archive-unpack/` is not one
of them.** `ADEPTHOOD_STAGING_RELDIRS` is exactly
`00-Creek-Meta/adepthood/journal` and `00-Creek-Meta/adepthood/uploads`:

```console
$ python -c "from creek.ingest.journal_staging import ADEPTHOOD_STAGING_RELDIRS, ARCHIVE_UNPACK_RELDIR; \
             print(ADEPTHOOD_STAGING_RELDIRS); print(ARCHIVE_UNPACK_RELDIR in ADEPTHOOD_STAGING_RELDIRS)"
(PosixPath('00-Creek-Meta/adepthood/journal'), PosixPath('00-Creek-Meta/adepthood/uploads'))
False
```

That tuple is what the right-to-be-forgotten sweep respects, in both
directions: `PurgeEngine._resolve_staged_source` discards any
`source.origin_key` that resolves outside it, and the whole-vault sweep
`_wipe_adepthood_staging` iterates it and nothing else. So:

- **`uploads/` and `journal/` are reachable by a scoped purge**, because
  the fragment carries a pointer into them. After the runs above:
  `01-Fragments/Notes/…-Hi.md` has `origin_key:
  00-Creek-Meta/adepthood/uploads/up-1-00f159f74f76.md`, and the journal
  fragment has `origin_key:
  00-Creek-Meta/adepthood/journal/adepthood-j-1-f326876331f3.md`.
- **`archive-unpack/` is *not* swept by purge — and does not need to be.**
  It was verified empty immediately after the `.zip` upload. The fragment
  the archive produced carries `origin_key: null`, so there is no staged
  copy for a scoped purge to chase in the first place.

See [cleaning-and-purge.md](./cleaning-and-purge.md) for what a purge does
with the fragments themselves.

## What was not verified here

Nothing below was executed while writing this page. It is listed so you do
not mistake silence for a working feature.

| Not verified | Why | Tracking |
|--------------|-----|----------|
| A real Google Drive OAuth grant, token exchange, or successful sync | No client secret and no browser; `creek gdrive --check` reported credentials `MISSING`. `POST /v1/connectors/drive/syncs` could only be observed refusing. | [#1568](https://github.com/Geoffe-Ga/Creek-Vault/issues/1568) |
| Whether a connected Drive sync ingests anything | Its success path is unreachable without live credentials. | [#1568](https://github.com/Geoffe-Ga/Creek-Vault/issues/1568) |
| LLM classification (`creek classify --method llm`) moving a fragment out of `unclassified` | No API key and no cloud consent were set. Every tier reported above is the tier **ingest itself** wrote. | — |
| Real `.pdf` and `.xlsx` documents | `.md`, `.txt`, `.csv`, `.py`, `.html`, `.rtf`, `.docx`, `.pptx`, `.json` and a `.zip` were all run. `.pdf` and `.xlsx` were not: no PDF library is installed in this environment (`pypdf`, `pdfplumber` and `pdfminer` all fail to import), so those two rows are enumerated from the routing table, not run. | — |
| Real OCR text extraction | `creek ingest --type image` over a real PNG fails here: *"The `tesseract` system binary was not found on PATH. The pytesseract Python package is a thin wrapper that shells out to it, so OCR cannot run without it."* The `platform: image_ocr` stamp and the low-confidence `review: pending_review` marker **were** executed, by injecting a stub `OcrEngine` (the documented extension point); the text extraction itself was not. | — |
| The MCP stdio transport end-to-end | `/v1` was exercised through a test client and `creek.upload` in-process. Per-tool MCP registration and the audit log were not spoken over the wire. | — |
| Idempotent re-ingest of *edited* sources, and `--incremental` / `--since` | A repeat run of unchanged markdown created nothing, consistent with idempotency, but the update-in-place branch was not tested. | [idempotent-ingest.md](./idempotent-ingest.md) |

## Related reading

### Inside creek-tools

- [ingestion.md](./ingestion.md) — the per-`--type` reference, and how
  `creek process` arbitrates when several ingestors claim the same file.
- [api.md](./api.md) — `/v1` setup, tokens, headers, contract versioning.
- [mcp.md](./mcp.md) — the MCP tool surface, including every gate on
  `creek.upload`.
- [configuration.md](./configuration.md#seeding-what-is-and-is-not-configurable)
  — which seeding knobs are real.
- [classification.md](./classification.md) — turning `unclassified` into a
  real tier.
- [cleaning-and-purge.md](./cleaning-and-purge.md) — deleting seeded material
  and its staged copies.

### The Adepthood half

Creek is one half of a pair. The application that seeds vaults on users'
behalf lives in `Geoffe-Ga/adepthood`, and the seeding work spans both
repositories:

- **This side:** epic
  [#1523](https://github.com/Geoffe-Ga/Creek-Vault/issues/1523) — the network
  seeding surface (`POST /v1/uploads`, archive upload, extension refusal, the
  Drive connector) — and its documentation child
  [#1528](https://github.com/Geoffe-Ga/Creek-Vault/issues/1528), which is
  this page.
- **That side:** the companion Adepthood **epic** is
  `Geoffe-Ga/adepthood#2250` — *"a user cannot put anything into the Higher
  Self corpus except what they type — no picker, and the endpoint
  refuses"*. Two of its children matter here:
  `Geoffe-Ga/adepthood#2252` is the **backend** child (*"implement
  `CreekVaultClient.upload` against the new `/v1` route and retire the
  refusal"*), and `Geoffe-Ga/adepthood#2255` is the **documentation**
  child, the Adepthood-side counterpart of this page. All three were open
  when this was written.

If you are reading this from Adepthood: the capability names an Adepthood
client checks for are the ones `GET /v1/capabilities` returns above —
`upload` and `drive-connector` are the two this epic added.
