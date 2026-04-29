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
   commit.
2. **Pre-commit hooks**: targeted hooks (ruff, mypy, refurb,
   tryceratops, vulture, interrogate, detect-secrets) gate staged
   files; the full battery still runs via `check-all.sh`.
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
| Pylint score               | ≥ 9.0     | `pylint creek/ --fail-under=9.0`              |
| MyPy                       | strict    | `mypy creek/` against `pyproject.toml`        |
| Cyclomatic complexity      | ≤ 10      | `xenon --max-absolute B`                      |
| pip-audit                  | 0 unhandled CVEs | `pip-audit` with documented `--ignore-vuln`  |

Every file referenced in `CLAUDE.md` resolves to a real path. Every
threshold mentioned in `CLAUDE.md` corresponds to a CI gate.

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
