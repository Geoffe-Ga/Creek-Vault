# ARCH-001: `creek/ingest/gdrive.py` is a downloader, not an Ingestor — breaks the four-stage contract claim

**Severity:** Low
**Category:** ARCH
**Estimated complexity:** M (≤1d)
**Parallelizable with peers in same category:** yes
**Discovered by:** Dimension 3

## Files affected
- `creek/ingest/gdrive.py` (entire module)
- `creek-tools/README.md:141-145` — claim "12 source platforms, each backed by a registered `Ingestor`"
- `creek-tools/docs/ingestion.md` — table lists `--type gdrive`

## Dependencies
INC-012 (the count). Pair them.

## Reproduction
```bash
grep -E "class .*\(.*Ingestor.*\)" creek/ingest/gdrive.py   # no hits
```

`creek/ingest/gdrive.py` defines `GoogleApiDriveClient` and `GoogleDriveDownloader` — neither inherits from `Ingestor` or implements the four-stage contract. The `creek gdrive` CLI command exists; `creek ingest --type gdrive` would expect an Ingestor that doesn't exist.

## Analysis

The CLI table in `docs/ingestion.md` includes `--type gdrive` as if it were a regular ingestor type. In reality, the gdrive flow is two-stage:
1. `creek gdrive --download --staging <dir>` mirrors files to local disk.
2. `creek ingest --type gdrive --input <dir>` is supposed to route the staged files through the appropriate ingestor (markdown / spreadsheet / presentation / etc.).

`route_to_ingestor` in `creek/ingest/gdrive.py` exists for step 2's routing. But there is no `GoogleDriveIngestor` class to register in `INGESTOR_REGISTRY` under the key `gdrive`. So `creek ingest --type gdrive` either fails or falls back to `generic`.

The architecture is fine in spirit (download separately, ingest with the per-format ingestors), but the user-facing surface lies — the README says "each backed by a registered `Ingestor`," and one of the twelve isn't.

Confidence: verified.

## Proposed remediation

Two options:
- **A.** Add a `GoogleDriveIngestor(Ingestor)` that implements `discover()` by calling `route_to_ingestor()` per file and dispatching to the underlying `MarkdownIngestor` / `SpreadsheetIngestor` / etc. Registers under `gdrive` so the CLI works as the docs imply.
- **B.** Remove `--type gdrive` from the docs and CLI. Tell users to run `creek gdrive --download` and then `creek ingest --type markdown --input <staging>` (etc.). Simpler, less magical.

Recommendation: B. The "ingest from a Drive staging dir" abstraction is leaky — the user already has to know which ingestor goes with which file type once the staging is local. Better to be explicit.

## Acceptance criteria

- The README and `docs/ingestion.md` count and ingestor list match the registry.
- `creek ingest --type gdrive` either works as a real ingestor (option A) or returns a clear "this type doesn't exist; use --type markdown/spreadsheet/etc. against the staging dir" message (option B).
- A test asserts the chosen behaviour.

## References
- `creek-tools/README.md:141-145`
- `creek-tools/docs/ingestion.md:8-22`
- `creek/ingest/gdrive.py`
- INC-012
