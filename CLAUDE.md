# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Repository Structure

`creek-tools` is a monorepo containing tooling for the Creek knowledge organization system:

- **`creek-tools/`** — Python subproject for the Creek processing pipeline. Has its own detailed `CLAUDE.md` with quality standards and workflow documentation. **Always read `creek-tools/CLAUDE.md` before working in that directory.**
- **`creek-tools/creek/templates/`** — Canonical templates deployed by `creek init`: `vault/` (folder scaffold), `skills/` (schema-skill tree), `AGENTS.md` (agent contract).
- **`docs/Ontology/creek_ontology_agent_prompt.md`** — Master specification for the Creek Ontology: a personal knowledge organization system built around Obsidian, the APTITUDE frequency framework, and the Archetypal Wavelength mapping.
- **No user vault content lives in this repo** (FEAT-019). The vault is scaffolded by `creek init --vault <path>` into a user-chosen location outside the repository.

## creek-tools Development

All commands run from the `creek-tools/` directory.

### Setup
```bash
cd creek-tools
uv sync --all-extras            # reproducible install from uv.lock
pre-commit install
```

`uv.lock` is the canonical, fully-pinned environment — local dev and CI
install from it so they never drift. After changing dependencies in
`pyproject.toml`, regenerate with `uv lock` and commit the result; CI
installs from the lock and fails the build on a stale lock. Plain `pip
install -r requirements-dev.txt` still works as an unpinned fallback.

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
./scripts/security.sh           # Bandit + pip-audit scans
./scripts/complexity.sh         # Radon/Xenon complexity analysis
./scripts/coverage-per-file.sh  # Per-file coverage gate (TEST-002)
./scripts/lint-extended.sh      # Optional: pylint, refurb, tryceratops, vulture, interrogate, shellcheck
                                # (not in check-all.sh; CI runs the subset that matters for the gate)
./scripts/pr-status.sh list        # List recent CI workflow runs
./scripts/pr-status.sh view ID     # View workflow run results
./scripts/pr-status.sh watch ID    # Watch workflow run progress
./scripts/pr-status.sh checks PR#  # Show PR check status
./scripts/pr-status.sh status PR#  # Full PR verdict (CI + Claude review)
```

### Running a Single Test
```bash
cd creek-tools
pytest tests/test_main.py -v
pytest tests/test_main.py::test_main_runs -v
```

### Quality Thresholds (non-negotiable)
- Test coverage: >=90% (branch coverage)
- Docstring coverage: >=95% (interrogate)
- Cyclomatic complexity: <=10 per function
- MyPy: strict mode, all functions typed
- Ruff (linting + formatting): zero violations

### Commit Conventions
- Uses [Conventional Commits](https://www.conventionalcommits.org/) enforced by pre-commit hook
- Pre-commit runs hooks including ruff (lint + format), mypy (strict), bandit, shellcheck, interrogate, vulture, detect-secrets, and more
- Direct commits to `main` are blocked by pre-commit; use feature branches

## Architecture

### creek-tools
- **Python >=3.11** (CI tests 3.11, 3.12, 3.13)
- Package source: `creek/` (flat layout, not src/)
- Tests: `tests/` (pytest with markers: `integration`, `e2e`)
- Config: `pyproject.toml` contains all tool configs (pytest, coverage, mypy, ruff, bandit)
- CI: `/.github/workflows/ci.yml` (at repo root; jobs use `working-directory: creek-tools`)
- Pre-commit: `creek-tools/.pre-commit-config.yaml` (install with `pre-commit install -c creek-tools/.pre-commit-config.yaml`)

### The Creek Ontology (docs/Ontology/)
The ontology prompt defines a complete system for organizing personal data into an Obsidian vault using five ontological primitives: **Fragments** (atomic content units), **Resonances** (semantic connections), **Threads** (narrative currents), **Eddies** (topic clusters), and **Praxis** (actionable insights). Content is classified along the 10-frequency APTITUDE system and the 6-phase Archetypal Wavelength cycle. This prompt is reference material for building the creek-tools pipeline (ingestion, classification, linking, voice proxy generation).

## Workflow: Stay Green

Follow the 4-gate process:
1. **TDD**: Write tests first, then implement
2. **Local**: `./scripts/check-all.sh` passes (exit 0)
3. **CI**: All GitHub Actions jobs green
4. **Review**: LGTM before merge

## Knowledge Graph (graphify) — query first

This repo publishes its code graph (~23k nodes) as assets on the rolling
`knowledge-graph` GitHub Release (never committed; part of the adepthood
federation). For ANY question about this codebase — structure, relationships,
impact — query the graph BEFORE grep/read sweeps:

- Restore once per session:
  `gh release download knowledge-graph --pattern graph.json --dir graphify-out`
  (or rebuild keyless: `pip install graphifyy && graphify extract . --code-only`).
- `graphify query "<question>"` · `graphify path "A" "B"` ·
  `graphify explain "X"` · `graphify affected "X"` (before changing X).
- Quote each cited node's `source_location`; verify before trusting.
- Fail-soft: if the CLI or graph is unavailable, proceed with normal file
  tools — never stall.
