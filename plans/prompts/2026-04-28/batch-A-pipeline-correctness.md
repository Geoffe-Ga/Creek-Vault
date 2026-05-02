# Batch A — Pipeline correctness rebuild

## Role

You are a senior Python engineer rebuilding the data path of an ingestion pipeline that currently drops user data on the floor. You write small, readable Pydantic-friendly code, you treat type-correctness as part of the contract, and you fix root causes — not symptoms.

## Goal

Make `creek process` actually move ingested content into the Obsidian vault, end to end, with deterministic IDs, real bodies, real platform routing, and surfaced errors. By the end of this batch, a markdown file in a source directory becomes a fragment file in the vault containing both correct frontmatter and the converted markdown body, and re-running the pipeline writes zero new files.

## Context

The five issues in this batch share root cause: the pipeline takes the four-stage ingestor's output, throws away everything except the source path, builds a stub `Fragment(title=parsed.source_path, source=FragmentSource(platform=SourcePlatform.OTHER))`, never calls the vault writer, never propagates errors, and uses a uuid4 default factory that hides the deterministic ID anyway. The vault writer compounds it by writing `frontmatter.Post(content="", **data)` — empty body — and only handling 6 of 12 source platforms.

**Read these issue files before starting** (in `plans/git-issues/`):
- `BUG-001-pipeline-discards-ingestor-output.md` — the central regression
- `BUG-005-pipeline-swallows-ingestor-errors.md` — `IngestResult.errors` never surfaced
- `BUG-007-fragment-default-uuid-vs-deterministic-id.md` — two parallel ID generators
- `BUG-008-vault-writer-stores-empty-body.md` — empty bodies in every written fragment
- `BUG-011-vault-writer-platform-mapping-incomplete.md` — 6 of 12 `SourcePlatform`s mapped

**Files you will primarily change:**
- `creek-tools/creek/pipeline.py` — `_run_ingestion`, `_run_classification`, `PipelineResult`
- `creek-tools/creek/vault/writer.py` — `_write_model`, `write_fragment`, `_PLATFORM_SUBFOLDER`
- `creek-tools/creek/models.py` — `Fragment.id` default factory
- `creek-tools/creek/ingest/base.py` — possibly extend `ParsedFragment` or the four-stage contract to cleanly carry `(Fragment, body_str)` to callers

**Files to consult (do not modify in this batch):**
- Every concrete ingestor under `creek/ingest/*.py` — confirm they all use `generate_fragment_id` and produce `parsed.metadata["frontmatter"]` consistently before you change the contract
- `creek/classify/rules.py`, `creek/classify/llm.py` — they consume `Fragment` objects; do not break their input contract

## Output format

A series of focused commits on a feature branch off `main`, each touching one of the five issues. Final state:

1. `Fragment.id` requires explicit input or computes deterministically — no uuid4 default for fragments produced by ingestors.
2. `Pipeline._run_ingestion` propagates platform, encoding, conversation_id/channel/interlocutor/author, the deterministic ID, and the converted markdown body all the way to a `VaultWriter.write_fragment(fragment, body=...)` call.
3. `PipelineResult.errors: list[str]` exists and is populated.
4. CLI `process` prints both fragment count and error count.
5. `VaultWriter.write_fragment` accepts a `body` parameter and writes it after the frontmatter block. Threads/Eddies/Praxis/Decisions get rendered bodies (one-paragraph summary is fine for now; a full template later).
6. `_PLATFORM_SUBFOLDER` covers every `SourcePlatform` enum value, with a test that asserts totality.

## Examples

A passing end-to-end test that should exist when this batch is done:

```python
def test_pipeline_writes_real_fragment(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "note.md").write_text("# A note\n\nBody text.\n", encoding="utf-8")
    vault = make_empty_vault(tmp_path / "vault")

    config = make_test_config(vault_path=vault)
    result = Pipeline(config=config).run(source_path=source, vault_path=vault)

    assert result.fragments_created == 1
    assert result.errors == []

    written = list((vault / "01-Fragments").rglob("*.md"))
    assert len(written) == 1
    post = frontmatter.load(str(written[0]))
    assert post["source"]["platform"] == "markdown"
    assert post["id"].startswith("frag-")
    assert len(post["id"].removeprefix("frag-")) == 12  # SHA-256[:12], not uuid8
    assert "Body text." in post.content     # not empty

    # Idempotency: re-running writes nothing new
    Pipeline(config=config).run(source_path=source, vault_path=vault)
    assert len(list((vault / "01-Fragments").rglob("*.md"))) == 1
```

That test currently has no equivalent and would have caught the regressions on day one. It must pass at the end of this batch.

## Requirements

- **Use `/stay-green`** for the implementation cycle: write the failing test first (start with the example test above), make it pass, refactor, re-check.
- **Use `/max-quality-no-shortcuts`** if you're tempted to add `# type: ignore`, `# noqa`, lower a coverage threshold, or wrap a real bug behind a `try/except` to make tests pass. The five issues have root-cause fixes; do them.
- Keep the four-stage ingestor contract recognisable — do not change its public shape across all 12 ingestors in this batch. If you need to widen `ParsedFragment` or add a sibling structure to carry `(Fragment, body)`, do so in `creek/ingest/base.py` only and let ingestors keep returning the same primitives they always have.
- Maintain `mypy --strict` clean (run via `./scripts/typecheck.sh`).
- Maintain ≥90% branch coverage. New code in `pipeline.py` and `vault/writer.py` should be at or above that threshold individually.
- Do **not** open any other batch's territory. Privacy filtering, audit logs, redaction patterns, etc. are explicitly out of scope here.
- Pre-existing tests that assert the broken behaviour (e.g., empty body, `platform=OTHER`) must be updated rather than waived.
- One commit per issue ID is preferred; commits reference the ID in the subject line (e.g. `fix(pipeline): propagate ParsedFragment metadata to VaultWriter (BUG-001)`).
- The end-to-end test above must live under `tests/e2e/test_pipeline_markdown_e2e.py` and be marked `@pytest.mark.e2e`.

## Definition of done

`./scripts/check-all.sh` exits 0; the new e2e test passes; coverage reports the touched files at ≥90%; running `creek process --source <markdown_dir> --vault <vault>` from a clean checkout produces a vault you can browse in Obsidian and read meaningful fragment bodies.
