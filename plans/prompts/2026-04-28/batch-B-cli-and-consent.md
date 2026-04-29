# Batch B — CLI surface and consent gate

## Role

You are a CLI ergonomics engineer wiring real engines to typer commands that have been printing "Would do X" for too long. You care about helpful error messages, predictable flag semantics, and UX that doesn't surprise the user. You enforce documented privacy contracts at the system boundary.

## Goal

Replace the four stub Typer commands (`creek ingest`, `creek classify`, `creek link`, `creek review`) with real implementations that call the underlying engines; gate `creek process` and `creek ingest` behind a `ConsentManager` per ontology spec §13.5; fix two pipeline bugs (`process` runs LLM unconditionally; `process` scans for redaction but never applies). When this batch is done, every command in the README's command-reference table works as documented.

## Context

`Batch A` (pipeline correctness) is a hard prerequisite: until BUG-001 / BUG-008 are fixed, the engines are full of stubs and there's nothing real to wire up. Confirm Batch A is merged before starting.

The six issues in this batch share a primary file (`creek-tools/creek/cli.py`) and a closely-coupled secondary file (`creek-tools/creek/pipeline.py`). They are the user's first impression of the tool: today, "creek ingest" prints a sentence and exits 0; that has to become a working pipeline command.

**Read these issue files before starting** (in `plans/git-issues/`):
- `INC-001-cli-stage-commands-are-stubs.md` — `creek ingest`, `creek classify`, `creek link` stubs
- `INC-002-creek-review-command-is-stub.md` — `creek review` stub
- `INC-010-consent-not-wired-into-cli.md` — `ConsentManager` exists but CLI never instantiates one
- `INC-011-link-rebuild-flag-and-classify-force-flag-missing.md` — `--force` and `--rebuild` flags
- `BUG-003-pipeline-runs-llm-after-rules-unconditionally.md` — bypasses `confidence_threshold`
- `BUG-004-pipeline-redaction-scans-but-never-applies.md` — sensitive data leaks into vault

**Files you will primarily change:**
- `creek-tools/creek/cli.py` — `process`, `ingest`, `classify`, `link`, `review`
- `creek-tools/creek/pipeline.py` — `_run_redaction`, `_run_classification`
- `creek-tools/creek/consent.py` — only if its API needs an additional method (avoid if possible)

**Files to consult (do not redesign in this batch):**
- `creek/classify/rules.py`, `creek/classify/llm.py`, `creek/classify/review.py`
- `creek/link/linker.py`, `creek/link/embeddings.py`
- `creek/redact/redactor.py`, `creek/redact/cli_commands.py`
- `creek/ingest/__init__.py` — `INGESTOR_REGISTRY`

## Output format

Six commits, each scoped to one issue, with subject prefixed by the issue ID. End state:

1. `creek ingest --type <name> --input <path> --vault <vault>` resolves the ingestor from the registry and writes real fragments. Idempotent on re-run.
2. `creek classify --vault <vault> --method <rules|llm> [--batch-size N] [--force]` actually runs the matching classifier; `--force` bypasses the `method: manual` preservation guard.
3. `creek link --vault <vault> --method <embeddings|temporal|eddies> [--rebuild]` runs the matching `LinkingPipeline` step; `--rebuild` invalidates the embeddings cache.
4. `creek review --vault <vault>` prints a navigable list of pending review-queue fragments and persists accept/override/defer decisions back to the fragment frontmatter as `classification.method: manual`.
5. `creek process` and `creek ingest` instantiate a `ConsentManager` from config and prompt for first-time consent for a source; `--yes` skips with a warning logged to the consent log.
6. `Pipeline._run_classification` only calls `LLMClassifier.classify` when the rule classifier left the fragment below `ClassificationConfig.confidence_threshold` or unclassified, and never on `auto_classify_sources` whose rules already produced high-confidence answers.
7. `Pipeline._run_redaction` either applies redactions when `redaction.enabled and not redaction.dry_run`, or refuses to ingest until the queue is clear (decide once, document in `docs/redaction.md`).

## Examples

A typical user session that should work end to end after this batch:

```bash
$ creek process --source /tmp/exports --vault ~/Creek-Vault
First time processing /tmp/exports.
Found: 1234 files, 47.2 MB, 89% .md / 11% .pdf.
Sample: notes/2025-01-04.md, notes/2025-01-12.md, ...
Proceed? [y/N]: y
Consent recorded.
Redaction: 17 secrets found and replaced.
Ingestion: 1234 fragments created (12 errors — see --verbose).
Classification: 1234 classified (rules), 89 escalated to LLM, 12 low-confidence -> review queue.
Linking: 4127 resonances, 23 threads, 7 eddies.
```

And a small unit test for the consent gate:

```python
def test_process_aborts_when_consent_declined(monkeypatch, tmp_path):
    ...
    monkeypatch.setattr("typer.confirm", lambda *_args, **_kw: False)
    result = runner.invoke(app, ["process", "--source", str(src), "--vault", str(vault)])
    assert result.exit_code == 1
    assert "consent" in result.output.lower()
    assert not (vault / "01-Fragments").iterdir()  # nothing ingested
```

## Requirements

- **Use `/stay-green`** for each command: write the failing CLI invocation test in `tests/test_cli*.py` first, then wire the engine call, then refactor.
- **Use `/max-quality-no-shortcuts`** if you're tempted to keep a `console.print("Would do X")` fallback path, or to swallow an engine exception with a `try/except: pass`.
- Honour the documented `--method` semantics exactly. `creek classify --method rules` must not reach for the LLM.
- For the redaction-in-process decision (BUG-004), **prefer the fail-loud option**: if `redact --apply` hasn't been run, `process` aborts with a clear remediation step. The auto-apply alternative is also acceptable but must respect `redaction.dry_run`. Document the chosen behaviour in `docs/redaction.md` and the `process` `--help` text.
- For consent (INC-010): non-interactive callers (no TTY) must pass `--yes` or fail. Log every consent grant with operator and timestamp.
- For the review TUI (INC-002): a numbered prompt list with `accept / override / defer / quit` is sufficient — a full Textual app is out of scope.
- Maintain `mypy --strict` clean and ≥90% branch coverage; add CLI integration tests under `tests/test_cli*.py` that use Typer's `CliRunner`.
- Do not redesign the consent model, the redaction queue, or the review queue in this batch. Wire the existing pieces.
- Do not implement `--include-tier` (that's Batch C, paired with SEC-006). Defer it.

## Definition of done

`./scripts/check-all.sh` exits 0; every command from the README's command-reference table works against a small synthetic vault; the e2e tests added in Batch A still pass; a new e2e test for the consent gate passes (declined consent → no ingestion, no fragments written).
