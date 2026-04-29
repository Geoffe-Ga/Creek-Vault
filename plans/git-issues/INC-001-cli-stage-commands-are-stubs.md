# INC-001: CLI single-stage commands `ingest`, `classify`, `link` are stubs

**Severity:** Critical
**Category:** INC
**Estimated complexity:** L (>1d)
**Parallelizable with peers in same category:** partial — wiring the three stubs to existing engines can be done in parallel, but they all touch `creek/cli.py`; expect merge serialization
**Discovered by:** Reading dimension 5 (incomplete functionality) — `creek/cli.py` review

## Files affected
- `creek/cli.py:54-64` — `ingest()` body is `console.print("Would ingest…")`
- `creek/cli.py:174-183` — `classify()` body is `console.print("Would classify…")`
- `creek/cli.py:186-194` — `link()` body is `console.print("Would link…")`

## Dependencies
Issues that must be fixed first: BUG-001 (the `Pipeline._run_ingestion` regression — fixing the three CLI stubs without first repairing the pipeline-level ingestion path will simply re-export the same broken behaviour). INC-002 (privacy-tier filter in generation) and INC-005 (consent gating) are independent.

## Blockers
Issues this one blocks: every workflow documented in `creek-tools/README.md` Quickstart and `docs/getting-started.md` step 5 ("Run individual stages") and `docs/classification.md` (workflow examples) and `docs/linking.md` (workflow examples). A user cannot follow any of those instructions today. Operationally, also blocks INC-006 (review queue in vault) since `creek classify` is the producer.

## Reproduction
```bash
$ creek ingest --type markdown --input /tmp/foo --vault /tmp/vault
Would ingest: type=markdown, input=/tmp/foo, vault=/tmp/vault
$ ls /tmp/vault/01-Fragments/
# empty — no fragments written
$ creek classify --vault /tmp/vault --method rules
Would classify: vault=/tmp/vault, method=rules, batch_size=50
$ creek link --vault /tmp/vault --method embeddings
Would link: vault=/tmp/vault, method=embeddings
```

The `process` command does call `Pipeline.run()`, so the pipeline is partially wired — but Quickstart and every doc page mixes `process` with single-stage calls, and the README's command reference table (lines 71-76 of `creek-tools/README.md`) advertises `creek ingest`, `creek classify`, `creek link` as first-class entries.

## Analysis

The Typer command bodies for `ingest`, `classify`, and `link` simply print a "Would do X" message and return. They do not call any of:
- `creek.ingest.INGESTOR_REGISTRY` ingestor classes
- `creek.classify.rules.RuleClassifier` / `creek.classify.llm.LLMClassifier`
- `creek.link.linker.LinkingPipeline`
- `creek.vault.writer.VaultWriter`

`creek process` (line 30-51) is the only command that actually wires the pipeline; even there, `Pipeline._run_ingestion` (`creek/pipeline.py:222-233`) silently discards the parsed fragment metadata and replaces it with a `Fragment(title=parsed.source_path, source=FragmentSource(platform=SourcePlatform.OTHER))` stub — see BUG-001.

This means the *advertised user workflow* (run scan → run apply → run process *or* per-stage commands → run report) does not exist. Per-stage iterative debugging is impossible. Re-classifying a vault after editing the keyword atlas (per `docs/classification.md` "Re-classifying after taxonomy changes") is a no-op.

This is the largest single launch blocker. Every other workflow gap downstream of these three commands is either masked by this issue or exposed by it.

Confidence: verified — read the entire `creek/cli.py` and `creek/pipeline.py`.

## Proposed remediation

For each of the three stubs:

1. **`creek ingest`** — Resolve `--type` against `INGESTOR_REGISTRY`, instantiate the matching `Ingestor`, call `ingest(source_path)`, then for each `ParsedFragment` build a real `Fragment` populated from `parsed.metadata` / `parsed.content` (preserving the deterministic ID returned by `creek/ingest/base.py:generate_fragment_id`) and call `VaultWriter.write_fragment`. Honour the optional `--vault` flag and fall back to `load_config().vault_path`. Honour `--input` similarly.

2. **`creek classify`** — Read every fragment from `<vault>/01-Fragments/`, dispatch on `--method`. For `rules`, call `RuleClassifier.classify` and write back. For `llm`, call `LLMClassifier.classify_batch` against fragments below `confidence_threshold`. Respect `--force` (preserve `method: manual`). Respect `--batch-size`. Update the review queue via `ReviewQueueGenerator.generate_queue`.

3. **`creek link`** — Dispatch on `--method` against `LinkingPipeline` for the four sub-linkers; honour the documented `--rebuild` flag (which is also missing — see INC-009).

Alternative: have `process` be the only "real" command and explicitly mark `ingest`/`classify`/`link` as removed. This would be a smaller change but would require deleting many doc pages and is at odds with the README command-reference table.

## Acceptance criteria

- `creek ingest --type markdown --input <dir> --vault <vault>` produces fragments under `<vault>/01-Fragments/` with deterministic IDs.
- Re-running the same command writes no new files (idempotency holds).
- `creek classify --vault <vault> --method rules` updates fragment frontmatter `classification.method: rules` and `classified_at`.
- `creek classify ... --method llm` only touches fragments with confidence below `confidence_threshold` or `method: unclassified`, unless `--force` is given.
- `creek link --vault <vault> --method embeddings` adds `links.resonances[]` to fragment frontmatter and writes thread/eddy notes when applicable.
- E2E test: `creek ingest && creek classify && creek link && creek report --type wavelength` succeeds against a synthetic vault.

## References
- `creek-tools/README.md` lines 65-117 (full command reference)
- `creek-tools/docs/getting-started.md` "Run individual stages"
- `creek-tools/docs/classification.md` workflow examples
- `creek-tools/docs/linking.md` workflow examples
- `00-Creek-Meta/Ontology/creek_ontology_agent_prompt.md` §9.2 (CLI Interface)
