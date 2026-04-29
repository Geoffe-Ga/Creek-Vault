# INC-010: Consent architecture exists but is not wired into the CLI

**Severity:** High
**Category:** INC
**Estimated complexity:** S (≤2h)
**Parallelizable with peers in same category:** yes
**Discovered by:** Dimension 5

## Files affected
- `creek/consent.py` — fully-built `ConsentManager`
- `creek/pipeline.py:73` — `Pipeline.__init__` accepts `consent_manager: ConsentManager | None = None`
- `creek/cli.py:30-51` — `process` command never constructs a `ConsentManager`

## Dependencies
None.

## Blockers
This is the spec's §13.5 "Consent Architecture" guarantee. Without it, every other privacy-related claim in the docs has a hole at the front door: data flows into the pipeline without ever asking permission.

## Reproduction
Read `creek/cli.py:44-45`:
```python
pipeline = Pipeline(config=config)
result = pipeline.run(source_path=source_path, vault_path=vault_path)
```

No consent manager is instantiated. `Pipeline._check_consent` returns `True` when `consent_manager is None`, so the consent gate is open by default in CLI usage.

## Analysis

Ontology spec §13.5:
> Before processing any data source for the first time, the CLI should:
> 1. Show a summary of what was found (file counts, date ranges, apparent content types)
> 2. Ask for explicit confirmation to proceed
> 3. Allow exclusion of specific files or date ranges
> 4. Record the consent in the Processing Log

The plumbing is there — `ConsentManager` produces summaries (`_build_source_summary`), records consent (`ConsentRecord` in `consent-log.json`), and `Pipeline` consults it. The CLI just never opts in.

Confidence: verified.

## Proposed remediation

In `creek/cli.py:process` (and once INC-001 is fixed, in `creek ingest` too):
- Construct a `ConsentManager(vault_path=...)` from the config.
- Call `consent_manager.prompt_for_consent(source_type, source_path)` (or the equivalent existing method) before pipeline execution.
- If declined, exit non-zero with a clear message.
- Add `--yes` / `--no-confirm` for non-interactive contexts (with a warning logged).
- Add `--exclude PATTERN` (matches §13.5 step 3).

## Acceptance criteria

- First-time `creek process --source <new>` prints a summary and prompts for confirmation.
- A subsequent run against the same source doesn't re-prompt.
- The consent log records each consent grant.
- `--yes` skips the prompt and logs a warning.
- A test simulates declining consent and asserts the pipeline does not run ingestion.

## References
- `00-Creek-Meta/Ontology/creek_ontology_agent_prompt.md` §13.5
- `creek/consent.py`
- `creek/pipeline.py:73, 117-132`
