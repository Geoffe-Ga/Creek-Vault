# DEP-002: `requirements.txt` makes the README's "lazy-imported optionals" hard requirements

**Severity:** High
**Category:** DEP
**Estimated complexity:** S (≤2h)
**Parallelizable with peers in same category:** yes
**Discovered by:** Dimension 8

## Files affected
- `creek-tools/requirements.txt:9-15` (`python-docx`, `pdfminer.six`, `markdownify`, `numpy`, `sentence-transformers`)
- `creek-tools/README.md:20-31` (the "optional dependencies are imported lazily" claim)

## Dependencies
DEP-001.

## Blockers
None.

## Reproduction
Run `pip install -r requirements.txt` per the README. You get ~120MB of transitive dependencies (scipy, scikit-learn pulled in by sentence-transformers; lxml by python-docx; etc.) regardless of which ingestors you use.

## Analysis

The README claim:
> Optional dependencies are imported lazily, so you only need to install the ones whose source types you actually ingest

…is true at the Python import level (most modules do `try: import X` inside functions), but `requirements.txt` defeats it by listing them as hard requirements. A user who only wants to ingest plain markdown ends up with the full ML stack on their machine.

## Proposed remediation

(Same shape as DEP-001 step 2.) Move the lazy-deps to `[project.optional-dependencies]` extras and provide named groupings:

- `documents` → `python-docx, pdfminer.six, markdownify`
- `embeddings` → `sentence-transformers, numpy`
- `ocr` → `pytesseract, pdf2image, pillow`
- `spreadsheets` → `openpyxl`
- `presentations` → `python-pptx`
- `gdrive` → `google-api-python-client, google-auth-oauthlib`
- `anthropic` → `anthropic`
- `all` → all of the above

Drop `requirements.txt` or shrink it to a single line that pulls extras for a chosen profile (e.g., `creek-tools[all]`).

Update README install instructions accordingly.

## Acceptance criteria

- `pip install -e .` (no extras) installs only the core 5 deps.
- `pip install -e .[markdown]` (or just core) is enough to run `creek ingest --type markdown`.
- `pip install -e .[ocr]` adds tesseract bindings; `creek ingest --type images` works.
- Lazy-import error messages are improved to point users at the right extra: "Install with `pip install creek-tools[ocr]`" rather than the current generic "Install with pip install pytesseract".

## References
- `creek-tools/README.md` "Optional dependencies"
- `creek-tools/requirements.txt`
- DEP-001
