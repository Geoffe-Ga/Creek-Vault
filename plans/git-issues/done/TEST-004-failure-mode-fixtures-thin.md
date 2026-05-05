# TEST-004: Failure-mode fixtures are minimal — corrupted exports, malformed YAML, mixed encodings, large files all untested

**Severity:** Medium
**Category:** TEST
**Estimated complexity:** M (≤1d)
**Parallelizable with peers in same category:** yes
**Discovered by:** Dimension 6

## Files affected
- `creek-tools/tests/fixtures/` (only 4 fixture files)

## Dependencies
None.

## Reproduction
```bash
ls creek-tools/tests/fixtures/
# sample_*.json, sample_fragment.md
```

Compare with the failure modes the system needs to handle gracefully:
- Corrupted ChatGPT export (truncated zip)
- Malformed YAML in fragment frontmatter (load and graceful fallback)
- Mixed encodings within a single CSV / Discord export
- Empty files
- Very large files (multi-MB Discord export, hundred-page PDF)
- Files with embedded `---` fences (prompt injection — see SEC-004)
- Symlinked files inside source dirs (see SEC-003)
- Concurrent ingestion of the same source

## Analysis

The fixtures directory is sparse. Most ingestor tests construct in-memory data programmatically rather than working from realistic example files. Realistic fixtures catch:
- Encoding bugs (BUG-010)
- Pydantic validation gaps when loading frontmatter from disk
- Edge cases in markdown conversion (smart quotes, weird unicode, stripped-but-then-re-emitted tags)

## Proposed remediation

Add a fixtures/ tree organised by failure mode:

```
tests/fixtures/
  corrupt/
    truncated_chatgpt.zip
    incomplete_pdf.pdf
    malformed_yaml.md
  encoding/
    cp1252.csv
    shift_jis.csv
    utf8_bom.csv
  scale/
    big_discord_export.json   (~10MB synthetic)
  injection/
    fragment_with_yaml_in_body.md
  symlinks/
    README.md  (instructions for setting up the symlink at test time)
```

Add corresponding tests under `tests/test_*_failure_modes.py` that exercise each path and assert graceful handling (clear error message, non-zero return, or ingest-with-warning).

## Acceptance criteria

- Each ingestor has at least one corrupt-input test case.
- Each ingestor has at least one encoding-edge-case test (where applicable).
- A fragment-with-injected-YAML test exists (also satisfies part of SEC-004).
- A symlink-in-source-dir test exists (pairs with SEC-003).
- Tests run in CI.

## References
- `creek-tools/tests/fixtures/`
- `tests/conftest.py`
