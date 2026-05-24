# ADR-0002: MIME-verification library choice for CrawDad attachments

- **Status**: Accepted
- **Date**: 2026-05-24
- **Driving issue**: FEAT-035 (#285)
- **Scope**: `crawdad/crawdad/attachments.py` only — the bot-side
  attachment gate. The `creek.ingest` pipeline (creek-tools side) does
  not perform a redundant check (explicit out-of-scope in #285).

## Context

FEAT-033 (PR #280) shipped extension-only gating in the CrawDad
attachment pipeline. A reviewer flagged the v1 limitation: a renamed
executable (`evil.exe` → `evil.md`) passes the allow list and reaches
the staging directory. The bot still requires user consent before any
`creek.ingest` call, and `creek.redact.scan` runs in between, so the
gap is bounded for the personal-tool deployment target. But the
"`ingest` was typed" signal is not the same as "the user knew what was
in the file". FEAT-035 adds a second, content-based check on top of
the extension allow list.

The bot is a small Python package shipped via `pip install crawdad`
into the operator's local environment. It runs on whatever the
operator has (a Raspberry Pi, a Docker container, a laptop). It must
not pull in a system-level library dependency that turns "I cloned the
repo" into a multi-step setup.

## Decision

CrawDad uses [`filetype`](https://pypi.org/project/filetype/) (pure
Python, BSD-3) for magic-byte detection on binary file types.

For text-extension files (`.md`, `.markdown`, `.txt`, `.html`,
`.htm`, `.json`, `.csv` — which have no magic byte signature) the
verifier runs a content sample instead: the first 1 KiB must decode
as UTF-8 and contain no NUL bytes. This catches the polyglot case the
issue calls out (executable or other binary blob renamed to a text
extension) without false-flagging legitimate UTF-8-encoded text.

The default `allowed_extensions` list shipped in `AttachmentConfig` is
narrowed to extensions whose content type the verifier can check:

- **Magic-byte verifiable**: `.pdf`, `.docx`, `.xlsx`, `.pptx`,
  `.png`, `.jpg`, `.jpeg`, `.gif`, `.webp`.
- **Text content-sampled**: `.md`, `.markdown`, `.txt`, `.html`,
  `.htm`, `.json`, `.csv`.

Legacy Office formats (`.doc`, `.xls`, `.ppt`) are dropped from the
default — `filetype` cannot reliably recognise OLE Compound documents
from their header, and the formats also carry macro-execution risk
that the personal-tool deployment target rightly avoids by default.
Operators who genuinely need them can re-add the extensions in
`crawdad.yaml`.

## Alternatives considered

### `python-magic` (libmagic wrapper)

- **Pros**: Mature, accurate, the canonical reference. Detects far
  more file types than `filetype`, and uses the same magic database as
  the venerable `file(1)` Unix tool.
- **Cons**: Requires the `libmagic` system library at runtime
  (`apt-get install libmagic1`, `brew install libmagic`, etc.). The
  `python-magic` package on PyPI does *not* bundle libmagic — that is
  `python-magic-bin`, which is Windows-only. So a Linux operator has
  to install a system package; a minimal Docker image needs a tweaked
  Dockerfile; CI matrix entries need an extra setup step. The bot has
  to fail clearly when libmagic is absent, adding error-handling
  surface.
- **Verdict**: Rejected. The cross-platform setup burden outweighs the
  marginal accuracy win for the file types CrawDad actually accepts.
  The Discord attachment surface is a short list of common formats —
  PDF, OOXML, images, text — all of which `filetype` handles correctly.

### `filetype` (pure Python)

- **Pros**: Pure Python, no system dependencies. Small (≈100 file
  types in its built-in signature table — exactly the common set
  CrawDad cares about). MIT-licensed, actively maintained. Two-call
  surface (`filetype.guess(data) → Type | None`) that fits behind the
  typed `MimeVerification` dataclass cleanly.
- **Cons**: Smaller signature set than `libmagic`. Cannot detect
  text-format mime types (which is why the verifier falls back to UTF-8
  + NUL-byte sampling for those). Does not have OOXML-specific
  detection — DOCX/XLSX/PPTX bytes are reported as `application/zip`
  (correct at the byte level; the verifier accepts both the canonical
  OOXML MIME and `application/zip` as a match for those extensions).
- **Verdict**: **Accepted.** Matches the CrawDad deployment target
  (small, single-user, pip-installable) without the system-package
  overhead.

### Custom signature table

- **Pros**: Zero dependencies. Full control over what is and isn't
  recognised.
- **Cons**: Re-implements a solved problem badly. Easy to get edge
  cases (RIFF / WEBP header parsing, OLE compound layout, PE
  signatures) subtly wrong. Every new format CrawDad accepts in future
  adds maintenance debt.
- **Verdict**: Rejected. Reinventing magic-byte detection for nine
  extensions earns nothing over a 50 KB dependency that handles them
  correctly already.

### `puremagic`

- **Pros**: Also pure Python, no system deps.
- **Cons**: Less actively maintained than `filetype` (last release
  March 2024 vs `filetype`'s May 2024 at writing). Returns a list of
  possible matches rather than a single best guess, which complicates
  the call site.
- **Verdict**: Rejected. `filetype` is the closer ergonomic fit.

## Consequences

**Positive**

- No system-package install step. `pip install crawdad` continues to
  produce a working bot.
- The polyglot case (zip-disguised-as-PDF, exe-renamed-to-md) now
  surfaces as a warning in the Discord safety report; operators can
  flip `reject_on_mime_mismatch: true` in `crawdad.yaml` to harden
  the gate further.
- Legacy Office (`.doc` / `.xls` / `.ppt`) removed from the default
  allow list — operators who don't actively need them no longer ship
  the macro-execution risk.

**Negative**

- The default text-extension verifier is heuristic (UTF-8 + NUL-byte
  check). A genuinely binary file that happens to contain no NUL bytes
  in its first 1 KiB and decodes as UTF-8 would pass — extremely
  unlikely in practice for the formats the operator is allow-listing,
  but not zero. A future hardening could parse a JSON file end-to-end
  for `.json`, run an HTML parser tolerantly over `.html`, etc., at
  the cost of more CPU and a wider failure surface.
- `filetype` does not ship `py.typed`, so MyPy needs an
  `ignore_missing_imports` override for the one module that imports
  it. The override is documented in `crawdad/pyproject.toml` next to
  the existing test-only override.
- DOCX / XLSX / PPTX may be reported by `filetype` as
  `application/zip` rather than their canonical OOXML MIME, depending
  on the runtime version. The verifier's acceptable-MIME set for
  those extensions includes both, so this is invisible to the user.

## Out of scope

- Anti-malware scanning (out of scope for a personal knowledge tool).
- Full archive inspection (zip / tar contents).
- MIME verification on the `creek.ingest` side — explicitly excluded
  by FEAT-035. The check stays at the bot's download gate where the
  file is first introduced.
- Cross-bot audit logging through `MCPAuditLog`. The bot logs every
  mismatch at WARNING via the standard Python logger; a future
  iteration may extend `creek.redact.scan` to accept structured
  mismatch metadata and persist it through the existing `MCPAuditLog`
  surface. Tracked as a follow-up; not blocking for FEAT-035.
