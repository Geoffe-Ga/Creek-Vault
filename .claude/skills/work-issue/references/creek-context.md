# Creek-Tools Project Context

Reference material for agents working on Creek issues. Read this before starting any issue.

## Repository Layout

```
Creek-Vault/
├── creek-tools/                 # Python subproject (all dev happens here)
│   ├── creek/                   # Package source (flat layout)
│   │   ├── cli.py               # Typer CLI (creek command)
│   │   ├── config.py            # Pydantic Settings (CreekConfig)
│   │   ├── models.py            # Ontological primitives (Fragment, Thread, Eddy, etc.)
│   │   ├── fragment.py          # FragmentationEngine (split/group documents)
│   │   ├── pipeline.py          # Pipeline orchestrator (redact -> ingest -> classify -> link -> index)
│   │   ├── redact/              # PII detection and redaction
│   │   ├── ingest/              # Source-specific ingestors (Discord, Claude, ChatGPT, Markdown)
│   │   ├── classify/            # Rule-based + LLM classification
│   │   ├── link/                # Linking pipeline (embeddings, temporal, threads, eddies)
│   │   ├── clean/               # Deduplication and quality scoring
│   │   ├── vault/               # Obsidian vault writer
│   │   └── generate/            # Index generation
│   ├── tests/                   # pytest test suite
│   ├── scripts/                 # Quality check scripts
│   ├── pyproject.toml           # All tool configs
│   └── .pre-commit-config.yaml  # Pre-commit hooks
├── .claude/skills/              # Claude Code skills
├── .github/workflows/           # CI pipeline
└── CLAUDE.md                    # Root instructions
```

## Key Commands (run from creek-tools/)

```bash
./scripts/check-all.sh          # ALL quality checks (must exit 0)
./scripts/fix-all.sh            # Auto-fix linting + formatting
./scripts/test.sh               # Unit tests
./scripts/test.sh --all         # All test types
./scripts/test.sh --coverage    # With coverage report
./scripts/lint.sh               # Ruff linting (--fix to auto-fix)
./scripts/format.sh --check     # Check formatting (--fix to apply)
./scripts/typecheck.sh          # MyPy strict
./scripts/security.sh           # Bandit + pip-audit
./scripts/complexity.sh         # Radon/Xenon
```

## Quality Thresholds (non-negotiable)

| Metric | Threshold | Tool |
|--------|-----------|------|
| Test coverage | >= 90% (branch) | pytest-cov |
| Docstring coverage | >= 95% | interrogate |
| Cyclomatic complexity | <= 10 per function | radon/xenon |
| MyPy | Strict mode, all typed | mypy |
| Ruff | Zero violations | ruff |
| Bandit | Zero vulnerabilities | bandit |

## Conventions

- **Python >= 3.11** (CI tests 3.11, 3.12, 3.13)
- **Conventional Commits**: `feat(scope):`, `fix(scope):`, `refactor(scope):`
- **No direct commits to main** (enforced by pre-commit hook)
- **All functions must have type annotations** (mypy strict)
- **All public functions must have docstrings** (interrogate >= 95%)

## Creek Domain Model

The Creek Ontology organizes personal knowledge using 5 primitives:
- **Fragment**: Atomic content unit (a message, a journal entry, a note)
- **Resonance**: Semantic connection between fragments (via embeddings)
- **Thread**: Narrative current across time (a topic you keep returning to)
- **Eddy**: Dense cluster of fragments without temporal direction
- **Praxis**: Actionable insight derived from patterns

Content is classified along:
- **APTITUDE Frequencies** (F1-F10): What the content is about
- **Archetypal Wavelength**: Phase, Mode, Orientation, Dosage
- **Voice Register**: How the content sounds (Confessional, Analytical, etc.)

## Common Patterns

### Adding a New Module

1. Create `creek/new_module/` with `__init__.py`
2. Create the main class file(s)
3. Create `tests/test_new_module.py`
4. Add any new config to `creek/config.py`
5. Wire into `creek/pipeline.py` if it's a pipeline stage
6. Add CLI command to `creek/cli.py` if needed

### Enhancing an Existing Module

1. Read existing code thoroughly
2. Read existing tests to understand current behavior
3. Add new tests FIRST (TDD)
4. Implement changes
5. Verify backward compatibility (existing tests still pass)

### Adding Dependencies

1. Add to `pyproject.toml` under `[project.dependencies]`
2. Add to `requirements-dev.txt` if dev-only
3. Run `pip install -e .` to update local environment
4. Verify CI won't break (check pip-audit exceptions)
