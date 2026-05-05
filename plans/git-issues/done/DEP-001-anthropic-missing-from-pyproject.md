# DEP-001: `anthropic` is in `requirements.txt` but missing from `pyproject.toml [project] dependencies`

**Severity:** High
**Category:** DEP
**Estimated complexity:** S (≤2h)
**Parallelizable with peers in same category:** yes
**Discovered by:** Dimension 8

## Files affected
- `creek-tools/pyproject.toml:13-19`
- `creek-tools/requirements.txt:16`
- `creek/classify/llm.py:32` — `import anthropic`

## Dependencies
None. Pairs with DEP-002 (lazy-deps treated as required).

## Reproduction
```bash
cd creek-tools
python -m venv /tmp/venv
/tmp/venv/bin/pip install -e .
/tmp/venv/bin/python -c "from creek.classify.llm import LLMClassifier; LLMClassifier(...)"
# ImportError: No module named 'anthropic'
```

`pip install -e .` honours only `pyproject.toml`'s `[project] dependencies = [...]`, which lists `typer, rich, pydantic, httpx, tqdm`. `requirements.txt` has the broader set; users following the README's `pip install -e .` step get the narrower set and run into ImportError on first use.

## Analysis

A user following `creek-tools/README.md` step "Install" runs `pip install -e .` and the Anthropic classifier breaks. The lazy-import attempt in `creek/classify/llm.py` doesn't help here — `anthropic` is the *required* import, not optional, when the provider is `anthropic`.

The right fix depends on intent: is `anthropic` a hard dep or an opt-in extra?
- The README treats it as opt-in: "or `ANTHROPIC_API_KEY` for the cloud path"
- The ontology spec calls cloud LLM "an explicit opt-in"
- But `creek/classify/llm.py` imports it at module load (line 32, no `try/except ImportError`)

## Proposed remediation

Two-part fix:
1. **Move `import anthropic` inside the Anthropic provider class** so the import only happens when the user opts into the Anthropic backend. Mirrors the lazy import pattern already used for Google API client and OpenPyXL.
2. **Restructure `pyproject.toml`** with `[project.optional-dependencies]`:
   ```toml
   [project.optional-dependencies]
   anthropic = ["anthropic>=0.40.0"]
   embeddings = ["sentence-transformers>=2.2.0", "numpy>=1.24.0"]
   ocr = ["pytesseract>=0.3", "pdf2image>=1.0", "pillow>=10.0"]
   documents = ["python-docx>=1.0", "pdfminer.six>=20221105", "markdownify>=0.11"]
   spreadsheets = ["openpyxl>=3.1"]
   presentations = ["python-pptx>=0.6"]
   gdrive = ["google-api-python-client>=2.0", "google-auth-oauthlib>=1.0"]
   all = [...]
   ```
3. **Drop `requirements.txt`** or keep it as a convenience wrapper (`-e .[all]`). Document `pip install -e .[anthropic]` in the README.

## Acceptance criteria

- `pip install -e .` works without `anthropic`. `creek classify --method rules` runs.
- `pip install -e .[anthropic]` installs the SDK. `creek classify --method llm` with `provider: anthropic` works.
- The `--method llm` path with `provider: ollama` does not require `anthropic`.
- README documents the extras.
- CI installs `pip install -e .[all]` so all optional code paths still get type-checked and tested.

## References
- `creek-tools/pyproject.toml`
- `creek-tools/requirements.txt`
- `creek/classify/llm.py:32`
- DEP-002
