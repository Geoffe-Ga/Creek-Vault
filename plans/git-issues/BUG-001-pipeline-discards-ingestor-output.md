# BUG-001: `Pipeline._run_ingestion` discards every ingestor's parsed metadata

**Severity:** Critical
**Category:** BUG
**Estimated complexity:** M (≤1d)
**Parallelizable with peers in same category:** yes — touches only `creek/pipeline.py`
**Discovered by:** Reading dimension 1 (bugs) — `creek/pipeline.py` review

## Files affected
- `creek/pipeline.py:222-233` — `_run_ingestion` body

## Dependencies
None — this fix is independent.

## Blockers
Blocks INC-001 (CLI stub repair, which is the user-facing manifestation), and every "process pipeline produces real fragments" downstream behaviour: classification, linking, indexing, voice-skill generation, mining, drafting, reports, purge-by-source, dedup. Effectively the entire pipeline.

## Reproduction
Run `creek process --source <some_dir> --vault <vault>` against any non-empty source. Inspect the produced fragments in `01-Fragments/`. Every fragment will have:
- `title` set to the **source file path string** (not a meaningful title)
- `source.platform = "other"` (not the actual ingestor's platform)
- All other classification, frequency, wavelength, voice fields at defaults
- Body content **missing** — `Fragment` does not carry body, but neither does the writer save the markdown produced by `convert_to_markdown`

## Analysis

```python
# creek/pipeline.py:222-233
for name, ingestor_cls in INGESTOR_REGISTRY.items():
    logger.info("Running ingestor: %s", name)
    ingestor = ingestor_cls()
    ingest_result = ingestor.ingest(source_path)
    for parsed in ingest_result.fragments:
        fragment = Fragment(
            title=parsed.source_path,
            source=FragmentSource(platform=SourcePlatform.OTHER),
        )
        fragments.append(fragment)
```

The `parsed` object is a `ParsedFragment` produced by the ingestor's full four-stage pipeline (`discover → parse → convert_to_markdown → generate_frontmatter`). It has `.content`, `.metadata` (which contains `markdown` and `frontmatter` keys per `creek/ingest/base.py:454-456`), `.timestamp`, and `.source_path`. None of these are propagated.

The constructed `Fragment`:
- Loses the deterministic ID computed in `creek/ingest/base.py:444-446`
- Loses the actual platform (markdown ingestor → `OTHER`, claude ingestor → `OTHER`, etc.)
- Loses encoding, conversation_id, channel, interlocutor, author
- Loses every classification stub the ingestor placed in `frontmatter`
- Loses the converted markdown body entirely

Pipeline never calls `VaultWriter.write_fragment` either, so even the broken `Fragment` is just held in a Python list and discarded at function exit. The only thing that survives is `result.fragments_created` count, which the CLI then prints.

So `creek process` is currently: scan-without-redact → log a fake count → run a no-op classification on stub fragments → run a no-op linking → generate index notes against an essentially empty vault.

Confidence: verified — read every line of `creek/pipeline.py`.

## Proposed remediation

Replace the `for parsed in ingest_result.fragments` loop with a path that:

1. Reads `parsed.metadata["markdown"]` and `parsed.metadata["frontmatter"]`.
2. Constructs a `Fragment` from the frontmatter dict (this is exactly what `Fragment.model_validate(...)` is for) — or, cleaner, change the ingestor contract to return real `Fragment` objects, with the body kept in a sibling structure.
3. Sets the deterministic `id = generate_fragment_id(parsed.source_path, parsed.timestamp, parsed.content)`.
4. Calls `VaultWriter.write_fragment(fragment)` immediately, passing the body text along — VaultWriter currently writes empty bodies (see `creek/vault/writer.py:283-285`); that's a related bug also worth fixing here.
5. Tracks per-ingestor stats and surfaces them on `PipelineResult`.

Alternative: split the four-stage contract so each ingestor returns `(Fragment, body_str)` tuples directly, eliminating the lossy `ParsedFragment` intermediary. More invasive but the ergonomics would improve.

## Acceptance criteria

- After `creek process --source <dir> --vault <vault>`, every fragment file in `<vault>/01-Fragments/` has:
  - `id: frag-<12 hex>` (deterministic; matches `generate_fragment_id` output)
  - Correct `source.platform` (markdown / claude / discord / etc.)
  - Non-empty body text (the converted markdown)
  - Source-specific frontmatter from `generate_frontmatter`
- Re-running the pipeline against the same source writes no new files (idempotency).
- `tests/test_pipeline.py` gains an integration test that confirms a markdown source ingests, classifies, and writes a fragment with the right platform and ID.

## References
- `creek/pipeline.py:222-233`
- `creek/ingest/base.py:226-242` (the deterministic ID generator that's getting dropped)
- `creek/ingest/base.py:444-466` (where the four-stage pipeline composes its output)
- `creek/vault/writer.py:151-167` (the writer that's never called)
