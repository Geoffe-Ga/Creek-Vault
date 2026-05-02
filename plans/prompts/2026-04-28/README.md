# Pre-launch remediation prompts — 2026-04-28

These eight batch prompts execute the remediation plan in `plans/git-issues/INDEX.md` (filed 2026-04-29). Each prompt covers one execution batch and follows the 6-component prompt-engineering structure (Role / Goal / Context / Output Format / Examples / Requirements).

## How to use

Each prompt is invoked alongside two skills that govern *how* the work is done:

- **`/stay-green`** — 2-gate TDD workflow (Red-Green-Refactor + pre-commit quality checks). Gate 1 is the failing test; Gate 2 is `./scripts/check-all.sh` exiting 0. Each prompt names the test that must fail first.
- **`/max-quality-no-shortcuts`** — anti-bypass philosophy. When tempted to add `# noqa`, `# type: ignore`, or lower a coverage threshold, fix the root cause instead.

The prompts assume both skills are active; they do **not** restate those skills' contents. Each prompt names the issue files (under `plans/git-issues/`) and the source files involved, so the executing agent has enough context to make judgement calls without re-reading the entire repo.

## Batch sequencing

The recommended order matches the dependency graph in `plans/git-issues/INDEX.md` §3-§4:

| Order | Batch | When to start | When to finish |
|-------|-------|---------------|----------------|
| 1 (parallel) | **A** — Pipeline correctness | day 0 | before B |
| 1 (parallel) | **F** — CI / deps / tests | day 0 (e2e tests need to exist as A's safety net) | rolling |
| 2 | **B** — CLI surface and consent | after A | before C/D/G |
| 3 (parallel) | **C** — Audit / privacy substrate | after A; ideally after B | before launch |
| 3 (parallel) | **D** — Redaction patterns | day 0 (independent) | before launch |
| 3 (parallel) | **E** — Vault performance | day 0 (independent) | before scale launch |
| 3 (parallel) | **G** — Security hygiene | day 0 (independent) | before launch |
| 4 | **H** — Operational polish | after A–G land | last |

## Batches and the issues they close

- **A** (`batch-A-pipeline-correctness.md`): BUG-001, BUG-005, BUG-007, BUG-008, BUG-011 — make the pipeline actually move data
- **B** (`batch-B-cli-and-consent.md`): INC-001, INC-002, INC-010, INC-011, BUG-003, BUG-004 — wire CLI commands to real engines, gate consent, fix two pipeline bugs
- **C** (`batch-C-audit-and-privacy-substrate.md`): SEC-005, INC-004, INC-005, INC-015, SEC-006, INC-007, PERF-002 — tamper-evident audit logs, privacy-tier filter, `--include-tier` flag
- **D** (`batch-D-redaction-patterns.md`): SEC-001, SEC-002, INC-009, INC-014, INC-016 — Luhn validation, missing pattern formats, configurable replacement template
- **E** (`batch-E-vault-performance.md`): PERF-001, PERF-003, PERF-004, BUG-006 — eliminate quadratic hotspots and the writer race
- **F** (`batch-F-ci-deps-tests.md`): DEP-001, DEP-002, DEP-003, CI-001, CI-002, CI-003, CI-004, TEST-001 through TEST-005, STYLE-001, STYLE-002 — toolchain alignment and test rigour
- **G** (`batch-G-security-hygiene.md`): SEC-003, SEC-004, SEC-007, SEC-008, OPS-002 — symlink guard, prompt injection, threat model, OAuth hygiene, purge-vault refusal
- **H** (`batch-H-operational-polish.md`): OPS-001, OPS-003, OPS-004, BUG-002, BUG-009, BUG-010, ARCH-001, ARCH-002, INC-003, INC-008, INC-012, INC-013, INC-017, INC-018 — the remaining cleanup

## Conventions in these prompts

- File paths are repository-relative (rooted at `Creek-Vault/`). The Python project lives under `creek-tools/`.
- Every prompt names a `Definition of done` section. Read it first if you only have time for one section.
- Every prompt explicitly lists what is **out of scope** for that batch. Resist the temptation to do more.
- "Use `/stay-green`" and "Use `/max-quality-no-shortcuts`" appear in every prompt's `Requirements` — they're cumulative, not optional.
- Tests added during a batch should live under the batch's natural file (`tests/test_pipeline.py`, `tests/test_redact.py`, `tests/e2e/...`) rather than being collected into a "batch X tests" module.

## Open questions

`plans/git-issues/INDEX.md` §6 lists 10 decision points the human should resolve before starting Batches C, D, F, and G. The prompts assume reasonable defaults (e.g., `OPEN` over `PUBLIC`, fail-loud over auto-apply for redaction-in-process) but flag the assumption inline.
