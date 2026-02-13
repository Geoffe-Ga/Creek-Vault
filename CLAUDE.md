# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Repository Structure

`creek-tools` is a monorepo containing tooling for the Creek knowledge organization system:

- **`creek-vault/`** — Python subproject for secure vault functionality. Has its own detailed `CLAUDE.md` with quality standards and workflow documentation. **Always read `creek-vault/CLAUDE.md` before working in that directory.**
- **`scripts/Ontology/`** — Contains `creek_ontology_agent_prompt.md`, the master specification for the Creek Ontology: a personal knowledge organization system built around Obsidian, the APTITUDE frequency framework, and the Archetypal Wavelength mapping.

## creek-vault Development

All commands run from the `creek-vault/` directory.

### Setup
```bash
cd creek-vault
pip install -r requirements-dev.txt
pre-commit install
```

### Key Commands (always use scripts, never run tools directly)
```bash
./scripts/check-all.sh          # Run ALL quality checks (do this before every commit)
./scripts/fix-all.sh            # Auto-fix linting + formatting
./scripts/test.sh               # Run unit tests
./scripts/test.sh --all         # Run all test types (unit, integration, e2e)
./scripts/test.sh --coverage    # Unit tests with coverage report
./scripts/coverage.sh           # Coverage report (--html for HTML output)
./scripts/lint.sh               # Ruff linting (--fix to auto-fix)
./scripts/format.sh --check     # Check formatting (--fix to apply)
./scripts/typecheck.sh          # MyPy strict type checking
./scripts/security.sh           # Bandit + Safety scans
./scripts/complexity.sh         # Radon/Xenon complexity analysis
```

### Running a Single Test
```bash
cd creek-vault
pytest tests/test_main.py -v
pytest tests/test_main.py::test_main_runs -v
```

### Quality Thresholds (non-negotiable)
- Test coverage: >=90% (branch coverage)
- Docstring coverage: >=95% (interrogate)
- Cyclomatic complexity: <=10 per function
- MyPy: strict mode, all functions typed
- Ruff + Black + isort: zero violations

### Commit Conventions
- Uses [Conventional Commits](https://www.conventionalcommits.org/) enforced by pre-commit hook
- Pre-commit runs hooks including ruff, black, isort, mypy (strict), bandit, safety, shellcheck, interrogate, vulture, detect-secrets, and more
- Direct commits to `main` are blocked by pre-commit; use feature branches

## Architecture

### creek-vault
- **Python >=3.11** (CI tests 3.11, 3.12, 3.13)
- Package source: `creek_vault/` (flat layout, not src/)
- Tests: `tests/` (pytest with markers: `integration`, `e2e`)
- Config: `pyproject.toml` contains all tool configs (pytest, coverage, mypy, ruff, bandit)
- CI: `.github/workflows/ci.yml` — quality checks, complexity analysis, build

### The Creek Ontology (scripts/Ontology/)
The ontology prompt defines a complete system for organizing personal data into an Obsidian vault using five ontological primitives: **Fragments** (atomic content units), **Resonances** (semantic connections), **Threads** (narrative currents), **Eddies** (topic clusters), and **Praxis** (actionable insights). Content is classified along the 10-frequency APTITUDE system and the 6-phase Archetypal Wavelength cycle. This prompt is reference material for building the creek-tools pipeline (ingestion, classification, linking, voice proxy generation).

## Workflow: Stay Green

Follow the 4-gate process:
1. **TDD**: Write tests first, then implement
2. **Local**: `./scripts/check-all.sh` passes (exit 0)
3. **CI**: All GitHub Actions jobs green
4. **Review**: LGTM before merge
