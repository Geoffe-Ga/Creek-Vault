## Role
You are a senior Python engineer working in this project's codebase, following
its existing conventions (TDD via stay-green, the `cd creek-tools &&
./scripts/check-all.sh` gate, ≥90% branch coverage aggregate and ≥80% per file,
≥95% docstring coverage, complexity ≤10, mypy strict, zero lint/type
suppressions).

## Goal
Close the behavioral coverage gaps in `creek/ingest/gdrive.py` (the largest
single-file gap in the pipeline at 84.66%, 57 uncovered lines): the
optional-dependency-absent error paths, the atomic-download temp-file cleanup
on mid-stream failure, and the `_drive_file_from_raw` timestamp/parent-path
normalisation. Verified by unit tests that mock the Google client seams and
assert the raised errors, the temp-file removal, and the parsed `DriveFile`
fields. Target: raise `gdrive.py` toward the 90% aggregate contribution and
remove it as a coverage laggard.

## Context
- File(s): `creek-tools/creek/ingest/gdrive.py`
- Scanned at commit: `82f9b89` — re-verify against HEAD before starting
- Evidence: `cd creek-tools && uv run pytest --cov=creek --cov=creek_mcp
  --cov-branch --cov-report=term-missing` reports:
  ```
  creek/ingest/gdrive.py  434  57  94  12  84.66%  51-53, 235, 238, 290->277, 327-354, 368, 371-373, 382-400, 416->418, 420, 448, 488-491, 551-557, 594-598, 650-651, 704->706, 708-709, 711, 752, 831, 1053, 1223
  ```
  High-value behavioral clusters:
  - **320-325 / 374-380** — `GoogleApiUnavailableError` is raised with the
    install-hint message when `googleapiclient` / `google-auth-oauthlib` are
    absent (the optional-dependency contract).
  - **339-354** — `_download`: the atomic write-to-`.download.tmp`-then-
    `os.replace` path, including the `except BaseException:` branch that unlinks
    the temp file and re-raises when `next_chunk()` fails mid-stream (so a
    partial download never masquerades as up-to-date).
  - **415-420** — `_drive_file_from_raw`: `modifiedTime` `Z`→`+00:00`
    normalisation and the naive-datetime tz-attach branch.
  - **382-400** — `_get_service`: the token-refresh vs. fresh-OAuth-flow branch
    selection (mock `Credentials`/`Request`/`InstalledAppFlow`).
- Related: sibling `[scan:coverage]` findings from the same run (documents.py
  #855, presentations.py #856, pipeline.py #868, spreadsheets.py, server.py,
  unnamed.py).

## Output Format
A single PR that: (1) adds a failing test first, (2) makes it pass, (3) passes
`cd creek-tools && ./scripts/check-all.sh`, and (4) references this issue with
"Closes #N".

## Examples
Exercise the download cleanup by injecting a fake `MediaIoBaseDownload` whose
`next_chunk()` raises, and assert the temp file is gone and the destination was
never created:

```python
def test_download_unlinks_temp_on_midstream_failure(monkeypatch, tmp_path):
    def boom(self):
        raise RuntimeError("network dropped")
    monkeypatch.setattr(gdrive, "MediaIoBaseDownload", _FakeDownloader(boom))
    dest = tmp_path / "out.bin"
    with pytest.raises(RuntimeError):
        connector._download(file_id="f1", export_mime=None, destination=dest)
    assert not dest.exists()
    assert not dest.with_name("out.bin.download.tmp").exists()
```
Prefer these mockable seams over standing up a real Drive service; do not add
network calls or credentials to the suite. For the OAuth branches, mock the
lazily-imported `google.*` symbols so no browser flow runs.

## Constraints
- Do not change public API signatures unless the Goal says so
- No lint/type suppressions (max-quality-no-shortcuts): fix root causes
- Assert on observable behavior (raised error type + message, file presence,
  parsed field values) — no assertion-free "coverage theater"
- Do NOT add live-network or credentialed tests; mock the Google client seams
- Scope: this issue only — file follow-up issues for adjacent problems
- If the finding no longer reproduces at HEAD, close this issue with a comment
  explaining what changed instead of forcing a PR
