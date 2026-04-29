# INC-013: `creek/clean/` submodules and `cleaning` config tree are undocumented for end users

**Severity:** Low
**Category:** INC
**Estimated complexity:** S (≤2h)
**Parallelizable with peers in same category:** yes
**Discovered by:** Dimension 5

## Files affected
- `creek/clean/{authorship,context,dedup,hygiene,markdown_filter,quality,semantic_dedup,validator}.py` — exist but not described in `docs/`
- `creek-tools/docs/cleaning-and-purge.md` — only documents the CLI surface, not the cleaning subsystem

## Dependencies
None.

## Reproduction
Compare:
- `creek/clean/` directory contents (8+ modules)
- `creek-tools/docs/cleaning-and-purge.md` (covers `creek clean orphans`, `clean stale-reviews`, `clean broken-links`, `clean duplicates`, `clean report`)

The CLI commands are documented; the underlying `authorship.py`, `context.py`, `validator.py`, `quality.py`, `markdown_filter.py`, `semantic_dedup.py`, `hygiene.py` are not — yet they all run during ingestion (via `creek/config.py:CleaningConfig` flags) and shape the user's data silently.

## Analysis

`docs/configuration.md` does describe `cleaning.discord`, `cleaning.chatbot`, `cleaning.markdown`, `cleaning.google_drive`, `cleaning.validation`, `cleaning.quality`, `cleaning.deduplication`, `cleaning.hygiene` config sections — but doesn't link them to specific modules or explain what tuning each knob does. A user inheriting a vault who finds that 30% of their fragments were dropped by the validator has no documentation to understand why.

This isn't a Critical issue — the system functions — but it's the kind of "tribal knowledge" gap that breeds bugs over time.

Confidence: verified.

## Proposed remediation

Add `creek-tools/docs/cleaning-pipeline.md` covering:
- The 8 cleaning modules and what each one does
- The order they run in during ingestion
- The config knobs that tune each
- How to disable a step
- Common symptoms and which knob to turn

Cross-reference from `docs/cleaning-and-purge.md` (which is about CLI hygiene, not the inline cleaning pipeline) and from `docs/configuration.md` (which lists the knobs without explaining the modules).

## Acceptance criteria

- New doc page exists.
- Each `creek/clean/*.py` module is mentioned and linked to its config section.
- The doc says when each step runs and what its failure mode is.

## References
- `creek/clean/`
- `creek-tools/docs/configuration.md` `cleaning` section
- `creek-tools/docs/cleaning-and-purge.md`
