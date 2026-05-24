# GAP-008 — No tests exercise mid-run interruption, LLM 5xx, or embedding-model load failure

- **Severity:** High
- **Prod-readiness criterion threatened:** crash recovery, unattended reliability

## Evidence

### What is covered

`tests/test_ingest_failure_modes.py` covers truncated JSON, malformed
YAML, empty files, mixed encodings, and a documented secret-lookalikes
sweep. `tests/test_classify_engine_resume.py` covers LLM-classify resume
from `llm-progress.jsonl` (OPS-001). `tests/test_purge.py` covers OSError
during audit-log migration. `tests/test_gdrive_downloader.py` covers
partial download failure, token-file unlink failure, and
unparseable-token recovery. The property suite
(`tests/test_properties_id.py`, `tests/test_properties_frontmatter.py`,
`tests/test_properties_redaction.py`) gives fuzz-like invariant coverage
on IDs, frontmatter round-trips, and redaction across 12 hypothesis
generators.

### What is not covered

```
$ grep -rn "KeyboardInterrupt\|SIGTERM\|SIGINT\|signal\." \
    creek-tools/tests/
(no results)
```

No test exercises mid-pipeline interruption — not in `test_pipeline.py`,
`test_classify.py`, `test_link.py`, or `test_compile.py`.

```
$ grep -rn "5xx\|HTTPStatusError\|httpx\.RequestError\|status_code=5\|429\b" \
    creek-tools/tests/
(no results worth quoting; only mock-fixture content references, no LLM
5xx scenario test)
```

The classify retry tests (`test_classify.py:test_retries_on_malformed_response`
and similar) cover malformed *content* from the LLM, not transport
failures. An Anthropic 5xx or Ollama-process-killed scenario is
untested. The failure-mode coverage audit confirmed this.

The sentence-transformers / embedding model-load failure is also
untested. `test_embeddings.py` exercises the cache layer with stubs;
`grep -n "ModelNotFoundError\|model.*unavailable\|SentenceTransformer.*raise" tests/`
returns nothing relevant.

PDF corruption / encrypted PDF: `test_ingest_documents.py` exercises
valid PDFs; encrypted/corrupt failure injection is absent.

`creek-tools/creek/pipeline.py` is 594 lines. `grep -n
"KeyboardInterrupt\|signal\." creek-tools/creek/pipeline.py` is empty
— there is no SIGINT handler. The pipeline's behavior on Ctrl-C is
"whatever the current iteration was doing leaves a partial state on
disk." This may be acceptable (atomic writes are used throughout the
save layer), but it is not asserted by any test.

## Why it matters

Unattended reliability — one of the four named v1 criteria — is the user
launching `creek process` against a 30k-fragment Drive mirror and going
to bed. The most likely failure modes on that overnight run are exactly:

- **Laptop sleeps / container reclaimed** → SIGTERM. Untested.
- **Anthropic API outage** → 5xx or timeout. Untested.
- **Ollama OOM-killed by the system** → connection refused. Untested.
- **Embedding model download fails** (first run, no network) → import
  error or timeout. Untested.

The pipeline may well degrade gracefully in some or all of these — the
classify-engine resume mechanism suggests it could — but without
coverage, the user discovers each new failure mode the hard way, and
each surfaces with a different error format and a different recovery
path. Honest claim: the pipeline is tested for *static* bad input but
not for *dynamic* environmental failures.

## Reproduction

Each scenario below is the outline of a missing test, not a bug today
— the bug today is the absence of the test:

```python
# tests/test_pipeline_interrupt.py
def test_classify_engine_progress_file_is_consistent_after_keyboard_interrupt(tmp_path):
    vault = _seed_vault_with_n_fragments(tmp_path, n=50)
    engine = ClassifyEngine(vault, method="llm", llm=_count_then_raise_keyboardinterrupt(after=25))
    with pytest.raises(KeyboardInterrupt):
        engine.run()
    progress = (vault / "00-Creek-Meta" / "Processing-Log" / "llm-progress.jsonl").read_text().splitlines()
    # Either each completed line is well-formed JSON, or none is — never a half-line.
    for line in progress:
        json.loads(line)
    # And resuming completes the rest without re-classifying any of the first 25.
```

```python
# tests/test_classify_llm_5xx.py
def test_classify_records_actionable_error_on_anthropic_5xx(tmp_path, monkeypatch):
    vault = _seed_vault(tmp_path)
    monkeypatch.setattr(
        "creek.classify.llm._anthropic_call",
        lambda *a, **kw: (_ for _ in ()).throw(httpx.HTTPStatusError("500", request=..., response=...))
    )
    engine = ClassifyEngine(vault, method="llm")
    result = engine.run()
    assert result.failed
    assert "Anthropic returned 500" in result.error_message  # or whatever the contract is
```

```python
# tests/test_embeddings_model_load.py
def test_clear_error_when_sentence_transformer_unavailable(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "sentence_transformers.SentenceTransformer",
        lambda *a, **kw: (_ for _ in ()).throw(OSError("model not downloaded"))
    )
    with pytest.raises(EmbeddingModelUnavailableError, match="sentence-transformers"):
        compute_embeddings_for_vault(tmp_path)
```

## Acceptance criteria

Closed when:

1. At least one test exercises a `KeyboardInterrupt` raised inside the
   `classify_engine` loop and asserts the progress JSONL file is
   line-by-line valid JSON (no torn writes).
2. At least one test exercises an LLM transport-level error (5xx or
   timeout) from both the Anthropic path and the Ollama path, and
   asserts the pipeline either (a) records the failure to the
   processing log and continues, or (b) fails loud with an actionable
   error message — whichever the contract chooses. The contract is
   documented in `docs/classification.md`.
3. At least one test exercises a `sentence-transformers` load failure
   and asserts a typed error (`EmbeddingModelUnavailableError` or
   equivalent) with a message that names the model and the
   remediation, rather than a `OSError`/`ImportError` stack trace from
   deep inside the library.
4. The pipeline doc (`docs/getting-started.md` or
   `docs/configuration.md`) names the documented behavior on each
   failure mode so a user knows what to expect.
5. Optional but recommended: a test for an encrypted PDF passed to the
   document ingestor, asserting it is skipped with a clear error
   rather than crashing the run.

## Files affected

- `creek-tools/tests/test_pipeline.py` (or new `test_pipeline_interrupt.py`)
- `creek-tools/tests/test_classify.py` (or new `test_classify_llm_failure.py`)
- `creek-tools/tests/test_embeddings.py` (or new
  `test_embeddings_model_load.py`)
- `creek-tools/creek/link/embeddings.py` (typed error if not present)
- `creek-tools/creek/classify/llm.py` (transport-error handling
  contract if not present)
- `creek-tools/docs/classification.md` (documented behavior)
- `creek-tools/docs/getting-started.md` (user-facing remediation)

## Dependencies / blockers

None. Pure test additions plus, possibly, a typed
`EmbeddingModelUnavailableError`. Coordinate with whoever owns the
classify-LLM contract before deciding on (a) record-and-continue vs.
(b) fail-loud — both are defensible; pick one and write the test.
