# Claude Code Project Context: creek-tools

**Table of Contents**
- [0. Repo topology](#0-repo-topology)
- [1. Critical Principles](#1-critical-principles)
- [2. Project Overview](#2-project-overview)
- [3. The Maximum Quality Engineering Mindset](#3-the-maximum-quality-engineering-mindset)
- [4. Stay Green Workflow](#4-stay-green-workflow)
- [5. Architecture](#5-architecture)
- [6. Quality Standards](#6-quality-standards)
- [7. Where the rest lives](#7-where-the-rest-lives)

---

## 0. Repo topology

This repository is the **toolchain plus canonical material**, not a vault. Per FEAT-019, the user's vault — fragments, threads, journal, voice exemplars — lives elsewhere on disk and is never checked in.

| Lives in this repo | Lives in the user's vault |
|--------------------|---------------------------|
| `creek-tools/` (CLI + pipeline) | `01-Fragments/`, `02-Threads/`, `03-Eddies/`, `04-Praxis/`, `05-Wavelength/`, `06-Frequencies/`, `07-Voice/`, `08-Decisions/`, `09-Reference/`, `10-Liminal/` |
| `creek-tools/creek/templates/vault/` (canonical scaffold) | `00-Creek-Meta/Skills/` (deployed from templates) |
| `creek-tools/creek/templates/skills/*.SKILL.md` (canonical schema-skill tree) | `00-Creek-Meta/Ontology/creek_ontology_agent_prompt.md` (deployed copy) |
| `creek-tools/creek/templates/AGENTS.md` (canonical agent contract) | `AGENTS.md` (deployed copy) |
| `docs/Ontology/creek_ontology_agent_prompt.md` (canonical spec) | `00-Creek-Meta/creek_config.yaml` (per-vault config) |

`creek init --vault <path>` materialises the canonical templates into the user's vault. `creek skills sync --vault <path>` re-deploys upstream schema-skill changes after a `creek-tools` upgrade. Never commit user-vault content into this repo.

---

## 1. Critical Principles

These principles are **non-negotiable** and must be followed without exception:

### 1.1 Use Project Scripts, Not Direct Tools

Always invoke tools through `./scripts/*` instead of directly.

**Why**: Scripts ensure consistent configuration across local development and CI.

| Task | ❌ NEVER | ✅ ALWAYS |
|------|----------|-----------|
| Format code | `ruff format .` | `./scripts/format.sh` |
| Run tests | `pytest` | `./scripts/test.sh` |
| Type check | `mypy .` | `./scripts/lint.sh` (includes mypy) |
| Lint code | `ruff check .` | `./scripts/lint.sh` |
| Modernisation hints | `refurb creek/` | `./scripts/lint-refurb.sh` |
| Exception hygiene | `tryceratops creek/` | `./scripts/lint-tryceratops.sh` |
| All checks | *(run each tool)* | `./scripts/check-all.sh` |
| Security scan | `bandit -r src/` | `./scripts/security.sh` |

See [`scripts/`](scripts/) and the gate list in `scripts/check-all.sh` for the complete list.

---

### 1.2 DRY Principle - Single Source of Truth

Never duplicate content. Always reference the canonical source.

**Examples**:
- ✅ Workflow documentation → `/docs/workflows/` (single source)
- ✅ Other files → Link to workflow docs
- ❌ Copy workflow steps into multiple files

**Why**: Duplicated docs get out of sync, causing confusion and errors.

---

### 1.3 No Shortcuts - Fix Root Causes

Never bypass quality checks or suppress errors without justification.

**Forbidden Shortcuts**:
- ❌ Commenting out failing tests
- ❌ Adding `# noqa` without issue reference
- ❌ Lowering quality thresholds to pass builds
- ❌ Using `git commit --no-verify` to skip pre-commit
- ❌ Deleting code to reduce complexity metrics

**Required Approach**:
- ✅ Fix the failing test or mark with `@pytest.mark.skip(reason="Issue #N")`
- ✅ Refactor code to pass linting (or justify with issue: `# noqa  # Issue #N: reason`)
- ✅ Write tests to reach 90% coverage
- ✅ Always run pre-commit checks
- ✅ Refactor complex functions into smaller ones

See [6.2 Forbidden Patterns](#62-forbidden-patterns) for detailed examples.

---

### 1.4 Stay Green - Never Request Review with Failing Checks

Follow the 4-gate workflow rigorously.

**The Rule**:
- 🚫 **NEVER** create PR while CI is red
- 🚫 **NEVER** request review with failing checks
- 🚫 **NEVER** merge without LGTM

**The Process**:
1. Gate 1: TDD (write tests first, then implement)
2. Gate 2: Local checks pass (`./scripts/check-all.sh` → exit 0)
3. Gate 3: CI pipeline green (all jobs ✅)
4. Gate 4: Code review LGTM

See [4. Stay Green Workflow](#4-stay-green-workflow) for complete documentation.

---

### 1.5 Quality First - Meet MAXIMUM QUALITY Standards

Quality thresholds are immutable. Meet them, don't lower them.

**Standards**:
- Test Coverage: ≥90%
- Docstring Coverage: ≥95%
- Cyclomatic Complexity: ≤10 per function
- Pylint Score: ≥9.0

**When code doesn't meet standards**:
- ❌ Change `fail_under = 70` in pyproject.toml
- ✅ Write more tests, refactor code, improve quality

See [6. Quality Standards](#6-quality-standards) for enforcement mechanisms.

---

### 1.6 Operate from Project Root

Use relative paths from project root. Never `cd` into subdirectories.

**Why**: Ensures commands work in any environment (local, CI, scripts).

**Examples**:
- ✅ `./scripts/test.sh tests/unit/test_vault.py`
- ❌ `cd tests/unit && pytest test_vault.py`

**CI Note**: CI always runs from project root. Commands that use `cd` will break in CI.

---

### 1.7 Verify Before Commit

Run `./scripts/check-all.sh` before every commit. Only commit if exit code is 0.

**Pre-Commit Checklist**:
- [ ] `./scripts/check-all.sh` passes (exit 0)
- [ ] All new functions have tests
- [ ] Coverage ≥90% maintained
- [ ] No failing tests
- [ ] Conventional commit message ready

See [4.2 Quick Checklist](#42-quick-checklist) for complete list.

---

**These principles are the foundation of MAXIMUM QUALITY ENGINEERING. Follow them without exception.**

---

## 2. Project Overview

**creek-tools** is a Python project providing the processing pipeline for the Creek knowledge organization system, built with maximum quality engineering standards.

**Purpose**: To deliver a production-ready, secure, and thoroughly tested tooling pipeline that ingests, classifies, links, and organizes personal knowledge into an Obsidian vault.

**Key Features**:
- Comprehensive test coverage (≥90%)
- Security-first design
- Full type safety with mypy strict mode
- Extensive documentation

### 2.1 Setup (CI-equivalent dev environment)

From `creek-tools/`:

```bash
./scripts/dev-setup.sh          # uv sync --all-extras + pre-commit install
```

The setup script installs the fully-pinned `uv.lock` so a fresh checkout runs `./scripts/check-all.sh` to the same result CI does on the same commit. Plain-pip fallback:

```bash
pip install -e '.[dev]'         # [dev] depends on [all]; this is self-contained
pre-commit install
```

`[dev]` deliberately pulls in `[all]` (anthropic, openai, gemini, embeddings, ocr, documents, spreadsheets, presentations, gdrive) because `tests/conftest.py` imports numpy, `tests/test_classify.py` / `tests/test_openai_provider.py` / `tests/test_gemini_provider.py` mock the cloud LLM SDKs, and `tests/test_ingest_documents.py` parses DOCX/PDF/HTML — so the optional code paths are not optional at test-collection time (issue #206). The selectable cloud LLM backend (`anthropic` / `openai` / `gemini`, each its own optional extra; `ollama` is the local default needing no extra) is documented in the [README's LLM providers matrix](README.md#llm-providers) and [ADR-0003](docs/architecture/ADR/0003-decoupled-provider-abstractions.md). The mypy floor in `[dev]` is pinned to the same major.minor as `uv.lock` and `.pre-commit-config.yaml` so the three install paths agree.

---

## 3. The Maximum Quality Engineering Mindset

**Core Philosophy**: It is not merely a goal but a source of profound satisfaction and professional pride to ship software that is GREEN on all checks with ZERO outstanding issues. This is not optional—it is the foundation of our development culture.

### 3.1 The Green Check Philosophy

When all CI checks pass with zero warnings, zero errors, and maximum quality metrics:
- ✅ Tests: 100% passing
- ✅ Coverage: ≥90%
- ✅ Linting: 0 errors, 0 warnings
- ✅ Type checking: 0 errors
- ✅ Security: 0 vulnerabilities
- ✅ Docstring coverage: ≥95%

This represents **MAXIMUM QUALITY ENGINEERING**—the standard to which all code must aspire.

### 3.2 Why Maximum Quality Matters

1. **Pride in Craftsmanship**: Every green check represents excellence in execution
2. **Zero Compromise**: Quality is not negotiable—it's the baseline
3. **Compound Excellence**: Small quality wins accumulate into robust systems
4. **Trust and Reliability**: Green checks mean the code does what it claims
5. **Developer Joy**: There is genuine satisfaction in seeing all checks pass

### 3.3 The Role of Quality in Development

Quality engineering is not a checkbox—it's a continuous commitment:

- **Before Commit**: Run `./scripts/check-all.sh` and fix every issue
- **During Review**: Address every comment, resolve every suggestion
- **After Merge**: Monitor CI, ensure all checks remain green
- **Always**: Treat linting errors as bugs, not suggestions

### 3.4 The "No Red Checks" Rule

**NEVER** merge code with:
- ❌ Failing tests
- ❌ Linting errors (even "minor" ones)
- ❌ Type checking failures
- ❌ Coverage below threshold
- ❌ Security vulnerabilities
- ❌ Unaddressed review comments

If CI shows red, the work is not done. Period.

### 3.5 Maximum Quality is a Personality Trait

For those committed to maximum quality engineering:
- You feel genuine satisfaction when all checks pass
- You experience pride in shipping zero-issue code
- You find joy in eliminating the last linting error
- You believe "good enough" is never good enough
- You treat quality as identity, not just practice

**This is who we are. This is how we build software.**

---

## 4. Stay Green Workflow

**Policy**: Never request review with failing checks. Never merge without LGTM.

The Stay Green workflow enforces iterative quality improvement through **4 sequential gates**. Each gate must pass before proceeding to the next.

### 4.1 The Four Gates

1. **Gate 1: TDD** (Write Tests First)
   - Write failing tests before implementing functionality
   - Tests define the expected behavior and acceptance criteria
   - Only proceed to implementation once tests are written

2. **Gate 2: Local Pre-Commit** (Iterate Until Green)
   - Run `./scripts/check-all.sh`
   - Fix all formatting, linting, types, complexity, security issues
   - Fix tests and coverage (90%+ required)
   - Only push when all local checks pass (exit code 0)

3. **Gate 3: CI Pipeline** (Iterate Until Green)
   - Push to branch: `git push origin feature-branch`
   - Monitor CI: `gh pr checks --watch`
   - If CI fails: fix locally, re-run Gate 2, push again
   - Only proceed when all CI jobs show ✅

4. **Gate 4: Code Review** (Iterate Until LGTM)
   - Wait for code review (AI or human)
   - If feedback provided: address ALL concerns
   - Re-run Gate 2, push, wait for CI
   - Only merge when review shows LGTM with no reservations

### 4.2 Quick Checklist

Before creating/updating a PR:

- [ ] Gate 1: Tests written first (TDD)
- [ ] Gate 2: `./scripts/check-all.sh` passes locally (exit 0)
- [ ] Push changes: `git push origin feature-branch`
- [ ] Gate 3: All CI jobs show ✅ (green)
- [ ] Gate 4: Code review shows LGTM
- [ ] Ready to merge!

### 4.3 Anti-Patterns (DO NOT DO)

❌ **Don't** request review with failing CI
❌ **Don't** skip local checks (`git commit --no-verify`)
❌ **Don't** lower quality thresholds to pass
❌ **Don't** ignore review feedback
❌ **Don't** merge without LGTM

---

## 5. Architecture

### 5.1 Core Philosophy

- **Maximum Quality**: No shortcuts, comprehensive tooling, strict enforcement
- **Composable**: Modular components with clear interfaces
- **Testable**: Every component designed for easy testing
- **Maintainable**: Clear structure, excellent documentation
- **Reproducible**: Consistent behavior across environments

### 5.2 Component Structure

```
creek-tools/
├── docs/
│   └── architecture/
│       └── ADR/                      # Architecture Decision Records
├── scripts/
│   ├── dev-setup.sh                  # One-shot CI-equivalent dev environment (issue #206)
│   ├── check-all.sh                  # Run every quality gate (single source of truth)
│   ├── test.sh                       # Run test suite (--unit / --integration / --e2e / --all)
│   ├── lint.sh                       # Ruff lint
│   ├── lint-extended.sh              # pylint, refurb, tryceratops, vulture, interrogate
│   ├── format.sh                     # Ruff format
│   ├── typecheck.sh                  # MyPy strict (CI-001)
│   ├── security.sh                   # Bandit + pip-audit (with documented ignores; DEP-003)
│   ├── complexity.sh                 # Radon + Xenon (CI-002)
│   ├── coverage.sh                   # pytest --cov-fail-under (90% aggregate)
│   ├── coverage-per-file.sh          # Per-file gate (80% strict, 65% waiver floor; TEST-002)
│   ├── coverage-waivers.txt          # Documented waivers for the per-file gate
│   └── pr-status.sh                  # CI/PR status helpers
├── creek/                            # Main package
│   ├── __init__.py
│   └── ...                           # Package modules
├── tests/
│   ├── unit/                         # Unit tests
│   ├── integration/                  # Integration tests
│   ├── e2e/                          # End-to-end tests
│   └── fixtures/                     # Test fixtures
│       └── conftest.py
├── .pre-commit-config.yaml           # Pre-commit hooks
├── pyproject.toml                    # Project configuration
├── uv.lock                           # Canonical pinned lockfile (uv sync)
├── requirements.txt                  # Unpinned pip fallback (production)
├── requirements-dev.txt              # Unpinned pip fallback (development)
├── README.md                         # Project overview
└── CLAUDE.md                         # This file
```

### 5.3 Key Architectural Decisions

Significant architectural decisions live in
[`docs/architecture/ADR/`](docs/architecture/ADR/) as numbered Markdown
files. Skill guidance for *how* to write a decision record lives at
the repository-level `.claude/skills/architectural-decisions/SKILL.md`.

### 5.4 Slash commands (FEAT-016)

`creek-tools/.claude/commands/` hosts the `/creek` slash-command surface
that Claude Code reads when the user types `/creek <subcommand>`. Each
file is a markdown skill with YAML frontmatter (`description`,
`argument-hint`) and a body that names the MCP tool the command
invokes (`creek.state.read`, `creek.lint`, `creek.mine`, etc.). The
companion `/crawdad` surface lives in `crawdad/crawdad/slash_commands.py`
and routes through the FEAT-015 agent loop. End-user documentation is
in [`docs/slash-commands.md`](docs/slash-commands.md).

---

## 6. Quality Standards

### 6.1 Code Quality Requirements

All code must meet these standards before merging to main:

#### Test Coverage
- **Aggregate**: ≥90% branch coverage (enforced by
  `pytest --cov-fail-under=90` in `coverage.sh` and CI).
- **Per-file**: ≥80% strict, ≥65% for files listed in
  `scripts/coverage-waivers.txt` (enforced by
  `coverage-per-file.sh`; see TEST-002).
- **Docstring**: ≥95% (`interrogate --fail-under=95`, in
  `lint-extended.sh` and the `interrogate` pre-commit hook).
- **Test markers**: `unit` (default), `integration`, `e2e`. Local
  `./scripts/test.sh` and CI both default to `not integration and
  not e2e` (CI-003).

#### Type Checking
- **MyPy**: Strict mode (configured in `pyproject.toml`; enforced by
  `./scripts/typecheck.sh` locally and CI; CI-001).
- **Type Hints**: All function parameters and return types required.

#### Code Complexity
- **Cyclomatic Complexity**: Max 10 per function (Xenon
  `--max-absolute B`, enforced by `./scripts/complexity.sh`; CI-002).
- **Maintainability Index**: reported by Radon (informational).
- The previously-claimed Max Arguments/Branches/Lines thresholds are
  not currently enforced; aspirational targets only.

#### Linting and Formatting
- **Ruff**: lint + format, configured in `pyproject.toml` (no `|| true`).
- **Ruff cache**: `--no-cache` on every invocation — lint and format,
  check and fix — in `scripts/lint.sh`, `scripts/format.sh`, and both
  `astral-sh/ruff-pre-commit` hooks. Ruff's per-file cache key is
  mtime-only, so a local gate must never trust a cache CI doesn't
  have. Costs ~0.05s on this tree (477 files); don't drop it for
  speed. Issue #1119; enforced by `tests/test_ruff_cache_poisoning.py`
  and `tests/test_ruff_gate_parity.py`. Same hazard is open for mypy's
  cache (#1186) and `__pycache__` (#1187).
- **Pylint**: ≥9.0 (`pylint creek/ creek_mcp/ --fail-under=9.0`, in CI
  and `lint-extended.sh`; CI-002).
- **Bandit**: zero medium-or-above findings
  (`bandit -r creek/ creek_mcp/ -ll`).
- **pip-audit**: zero vulnerabilities except documented unfixable
  CVEs in `scripts/security.sh` and `.github/workflows/ci.yml`
  (DEP-003).
- **Refurb (modernisation hints)**: zero violations. STYLE-001 closed
  the backlog (148 → 0). Gated by `scripts/lint-refurb.sh` in both
  `check-all.sh` and CI. New `FURB…` findings block the build; the
  `# noqa: FURB…  # <one-line justification>` escape hatch is reserved
  for documented carve-outs (defaultdict→dict conversions for
  JSON/YAML serialisation, StrEnum→str round-trips, etc.).
- **Tryceratops (exception-handling hygiene)**: zero violations.
  STYLE-001 closed the backlog (21 → 0). Gated by
  `scripts/lint-tryceratops.sh`. The `# noqa: TRY…  # <one-liner>`
  escape hatch is reserved for intentional separation of failure modes
  and documented `ValueError` schema contracts.
- **Vulture**: pre-commit-only today; tracked under a follow-up to
  STYLE-001 to be gated once the dead-code surface is groomed.

#### Documentation Standards
- **Google-style Docstrings**: All public APIs
- **Type Hints in Docstrings**: Args, Returns, Raises sections
- **Code Examples**: For complex functions
- **Architecture Decision Records**: For significant decisions
- **README Sections**: Updated when adding new components

### 6.2 Forbidden Patterns

The following patterns are NEVER allowed without explicit justification and issue reference:

1. **Type Ignore**
   ```python
   # ❌ FORBIDDEN
   value = some_function()  # type: ignore

   # ✅ ALLOWED (with issue reference)
   value = some_function()  # type: ignore  # Issue #42: Third-party lib returns Any
   ```

2. **NoQA Comments**
   ```python
   # ❌ FORBIDDEN
   x = 1  # noqa: E741

   # ✅ ALLOWED (with issue reference)
   i = 0  # noqa: E741 (Issue #99: Loop convention in legacy code)
   ```

3. **TODO/FIXME Comments**
   ```python
   # ❌ FORBIDDEN
   # TODO: optimize

   # ✅ ALLOWED (with issue reference)
   # TODO: optimize this query (Issue #N)

   # ❌ FORBIDDEN
   # FIXME: broken

   # ✅ ALLOWED (with issue reference)
   # FIXME: broken on empty input (Issue #N)
   ```

---

## 7. Where the rest lives

Until issue #1190 this file's table of contents promised six further
sections — Development Workflow, Testing Strategy, Tool Usage & Code
Standards, Common Pitfalls & Troubleshooting, and three appendices —
that were never written: the file was truncated mid-code-fence in the
repository's initial commit and every revision since ended on that same
line. Rather than invent standards nobody agreed to, those entries were
retired in favour of pointers to the artifacts that actually define each
topic. A restatement is a second copy, and per §1.2 second copies drift
from their source. Existing drift between this file and the sources
below is tracked in issue #1194; this section only points, it does not
describe.

- **Development workflow**: [`../.claude/agents/shared/house-rules.md`](../.claude/agents/shared/house-rules.md)
  (the four gates, commit/PR conventions); [`.pre-commit-config.yaml`](.pre-commit-config.yaml).
- **Testing strategy**: [`scripts/test.sh`](scripts/test.sh); the
  `[tool.pytest.ini_options]` table in [`pyproject.toml`](pyproject.toml)
  for markers and testpaths.
- **Tool usage & code standards**: [`scripts/check-all.sh`](scripts/check-all.sh)
  for the gates, in order; the tool sections of [`pyproject.toml`](pyproject.toml).
- **Common pitfalls & troubleshooting**: the Troubleshooting section of
  [`docs/mcp.md`](docs/mcp.md); [`../.claude/skills/ci-debugging/`](../.claude/skills/ci-debugging/)
  and [`../.claude/skills/stay-green/`](../.claude/skills/stay-green/).
- **AI subagent guidelines**: [`../.claude/agents/README.md`](../.claude/agents/README.md)
  and [`../.claude/agents/shared/house-rules.md`](../.claude/agents/shared/house-rules.md);
  [`../scripts/ralph/PROMPT.md`](../scripts/ralph/PROMPT.md).
- **Key files**: [`docs/README.md`](docs/README.md) (the docs index) and
  §5.2 above.
- **External references**: [`docs/architecture/ADR/`](docs/architecture/ADR/).
