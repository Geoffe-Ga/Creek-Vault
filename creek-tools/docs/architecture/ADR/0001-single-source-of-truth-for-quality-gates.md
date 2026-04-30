# ADR-0001: Single source of truth for quality gates

- **Status**: Accepted
- **Date**: 2026-04-29
- **Driving issues**: CI-001, CI-002, CI-003, CI-004, TEST-002,
  DEP-003, STYLE-002 (Batch F)

## Context

`creek-tools/CLAUDE.md` advertises a maximum-quality engineering bar
("Pylint ≥ 9.0", "MyPy strict", "Cyclomatic complexity ≤ 10",
"≥90% branch coverage", "Docstring coverage ≥ 95%", "no `|| true`").
Before Batch F the actual enforcement was scattered:

- `.github/workflows/ci.yml` ran MyPy with
  `--ignore-missing-imports --no-strict-optional`, plus `|| true`, plus
  `continue-on-error: true` (CI-001).
- Pylint and Xenon ran with `|| true` and `continue-on-error: true`
  (CI-002).
- Pytest ran without a marker filter, mixing integration into the unit
  matrix (CI-003).
- Pre-commit hooks invoked tools (refurb, tryceratops, vulture,
  shellcheck, detect-secrets) that CI never ran (CI-004).
- `scripts/security.sh` had no `--ignore-vuln` set, so it failed
  locally on transitive dev-tooling CVEs that CI explicitly ignored
  (DEP-003).
- The aggregate 90% coverage gate masked three modules below 80%
  (TEST-002).
- `CLAUDE.md` referenced `docs/skills/` and `docs/architecture/ADR/`
  paths that did not exist (STYLE-002).

The result: the project's promised quality gates were on the honour
system. Local checks could fail while CI passed, or vice-versa.

## Decision

The `creek-tools/scripts/check-all.sh` script is the **single source of
truth** for what "green" means. Three downstream consumers all invoke
the same scripts:

1. **Local development**: `./scripts/check-all.sh` exits 0 before any
   commit. It runs the **gating** subset (lint, format, typecheck,
   security, complexity, unit tests, coverage report, per-file
   coverage gate). Extended-only checks (refurb, tryceratops, vulture,
   detect-secrets baseline audit) live in
   `./scripts/lint-extended.sh` and are run on demand or by
   pre-commit; they will join `check-all.sh` once STYLE-001 clears
   the existing whole-tree backlog.
2. **Pre-commit hooks**: targeted hooks (ruff, mypy, refurb,
   tryceratops, vulture, interrogate, detect-secrets, pylint-fast)
   gate staged files. These run only on changed files, so they catch
   *new* violations cheaply without forcing the full backlog.
3. **CI** (`.github/workflows/ci.yml`): every step calls a project
   script (`./scripts/typecheck.sh`, `./scripts/test.sh --unit
   --coverage`, `./scripts/coverage-per-file.sh`,
   `./scripts/complexity.sh`). No inline tool invocations, no
   `|| true`, no `continue-on-error: true`.

Specific commitments:

| Gate                       | Threshold | Enforced by                                   |
|----------------------------|-----------|-----------------------------------------------|
| Aggregate branch coverage  | ≥ 90%     | `pytest --cov-fail-under=90` in `coverage.sh` |
| Per-file coverage          | ≥ 80%     | `coverage-per-file.sh` (waiver list under 65% floor) |
| Docstring coverage         | ≥ 95%     | `interrogate --fail-under=95`                 |
| Pylint score               | ≥ 9.0     | `./scripts/pylint.sh` (`PYLINT_FAIL_UNDER` env override) |
| MyPy                       | strict    | `mypy creek/` against `pyproject.toml`        |
| Cyclomatic complexity      | ≤ 10 per fn / module-avg ≤ 10 | `xenon --max-absolute B --max-modules B --max-average B` |
| pip-audit                  | 0 unhandled CVEs | `pip-audit` with documented `--ignore-vuln`  |

Every file referenced in `CLAUDE.md` resolves to a real path. Every
threshold mentioned in `CLAUDE.md` corresponds to a CI gate.

### Notes on the chosen Xenon thresholds

Xenon scores complexity on the radon scale: A=1–5, B=6–10, C=11–20,
D=21–30, E=31–40, F≥41. The previously-inline (and unenforced) CI step
used `--max-average A`; the project's local script always used
`--max-average B`. `complexity.sh` is the source of truth and runs
`--max-average B`, so CI now matches.

`--max-absolute B` (no single function above C) directly enforces the
≤10 per-function rule that `CLAUDE.md` advertises. `--max-average B`
tolerates a module whose *average* function complexity sits in the 6–10
band, which is more lenient than the old aspirational A but matches
what the codebase actually achieves today (verified by running
`xenon` against `creek/` at the time of writing). Tightening to
`--max-average A` is a reasonable follow-up once the existing surface
is refactored, but is out of scope for this ADR.

### Notes on the per-file coverage gate

`coverage-per-file.sh` reads `summary[file].summary.percent_covered`
from `coverage.json`, which is the *line* coverage percentage. The
aggregate gate (`pytest --cov-branch --cov-fail-under=90`) still
enforces branch coverage at the project level. Using line coverage at
the per-file level catches the most egregious "this module has no
tests at all" cases without false-failing on files whose every line is
exercised but whose branch state-space is large (ingestors with many
content-type heuristics are the canonical example). A future iteration
may switch to `percent_covered_branches`; the script supports it via a
single field-name change.

### Notes on `numpy` as a core runtime dependency

Reviewer feedback on PR #170 has repeatedly suggested moving
`numpy>=2.4.4` from `[project] dependencies` into the `[embeddings]`
extra, on the principle that heavy ML dependencies should be opt-in
the same way `markdownify` is for `[documents]`. The argument is
correct in spirit — `numpy` is ~20 MB and only directly relevant to
embedding-style functionality.

The reason it stays in the core dependency list for Batch F:

1. Five modules (`creek/clean/semantic_dedup.py`,
   `creek/link/embeddings.py`, `creek/link/threads.py`,
   `creek/link/eddies.py`, `creek/generate/unnamed.py`) all import
   `numpy` at module load.
2. `creek/link/__init__.py` re-exports the numpy-using submodules,
   and `creek/pipeline.py` does
   `from creek.link.linker import LinkingPipeline`. Therefore
   `import creek.pipeline` triggers `numpy` transitively at import
   time today.
3. Moving `numpy` to `[embeddings]` without refactoring those five
   modules would break `import creek.pipeline` for any user who
   installed `creek-tools` without the `[embeddings]` extra — a
   real usability regression for what is otherwise the core data
   pipeline.
4. The right architectural fix is to (a) wrap each numpy import in
   a clear `try/except ImportError` block pointing at
   `creek-tools[embeddings]`, and (b) lazy-import `LinkingPipeline`
   inside `Pipeline.__init__` so the pipeline gracefully degrades
   when the linking stage is unavailable. That refactor changes
   `creek.link`'s public contract (linking becomes opt-in instead
   of always available) and is therefore out of scope for Batch F
   (CI / deps / tests). It belongs in a follow-up batch focused on
   the linking architecture.

For now `numpy` is documented here as a deliberate exception to the
opt-in extras pattern, alongside the eager linking pipeline that
relies on it.

### Notes on the branch coverage threshold

Pre-Batch-F CI carried a separate `BRANCH_COVERAGE_THRESHOLD: 85` env
var that an inline Python step compared against
`percent_covered_branches`. The new flow drops the separate 85% floor
because `pytest --cov-branch --cov-fail-under=90` already counts
branches in the composite metric, and the composite at 90% is in
practice stricter than line coverage at 90% with a separate branch
floor at 85% (the composite must clear 90% with branches included, so
a project trending toward many uncovered branches would push the
composite below 90% before any branch-only gate would have fired).
The trade-off: a hypothetical file that adds many uncovered branches
while keeping the composite above 90% would be invisible. The per-
file gate at 80% bounds the worst case at the file level. If
follow-up data shows branch-only regressions slipping through, the
right fix is `[tool.coverage.report] fail_under_branch = 85`, not a
return to the inline Python check.

## Consequences

**Positive**

- A change that violates a documented threshold fails CI deterministically.
- Pre-commit, `check-all.sh`, and CI converge on the same verdict.
- Adding a new gate is a one-line addition to `check-all.sh`; no
  separate workflow edit required.
- Documentation drift surfaces as a real failure rather than as a
  silent contradiction between docs and toolchain.

**Negative**

- The CI workflow grew slightly more opinionated about the editable
  install (`pip install -e .[all,dev]`) instead of using ad-hoc
  `requirements.txt` flows.
- The per-file coverage gate currently grants waivers to two ingestor
  modules (`presentations.py`, `documents.py`). Those waivers carry
  open work in the form of an issue tracking the path to 80%.
- Extended-lint tools (refurb, tryceratops) are not yet in
  `check-all.sh` because the existing violation backlog (STYLE-001)
  needs to be cleared first; until then they remain pre-commit-only.

## Alternatives considered

- **Keep CI permissive, document aspirations as goals.** Rejected:
  this is the status quo that motivated the issue catalog.
- **Move all CI to a Makefile.** Rejected: bash scripts already exist,
  are tested by every developer, and don't pull in a new build tool.
- **Use `tox` to orchestrate gates.** Rejected: adds a layer between
  the developer and the underlying tools; the script-per-tool layout
  keeps each gate inspectable.
