# Claude Code Project Context: creek-tools

**Table of Contents**
- [1. Critical Principles](#1-critical-principles)
- [2. Project Overview](#2-project-overview)
- [3. The Maximum Quality Engineering Mindset](#3-the-maximum-quality-engineering-mindset)
- [4. Stay Green Workflow](#4-stay-green-workflow)
- [5. Architecture](#5-architecture)
- [6. Quality Standards](#6-quality-standards)
- [7. Development Workflow](#7-development-workflow)
- [8. Testing Strategy](#8-testing-strategy)
- [9. Tool Usage & Code Standards](#9-tool-usage--code-standards)
- [10. Common Pitfalls & Troubleshooting](#10-common-pitfalls--troubleshooting)
- [Appendix A: AI Subagent Guidelines](#appendix-a-ai-subagent-guidelines)
- [Appendix B: Key Files](#appendix-b-key-files)
- [Appendix C: External References](#appendix-c-external-references)

---

## 1. Critical Principles

These principles are **non-negotiable** and must be followed without exception:

### 1.1 Use Project Scripts, Not Direct Tools

Always invoke tools through `./scripts/*` instead of directly.

**Why**: Scripts ensure consistent configuration across local development and CI.

| Task | ❌ NEVER | ✅ ALWAYS |
|------|----------|-----------|
| Format code | `black .` | `./scripts/format.sh` |
| Run tests | `pytest` | `./scripts/test.sh` |
| Type check | `mypy .` | `./scripts/lint.sh` (includes mypy) |
| Lint code | `ruff check .` | `./scripts/lint.sh` |
| All checks | *(run each tool)* | `./scripts/check-all.sh` |
| Security scan | `bandit -r src/` | `./scripts/security.sh` |

See [9.1 Tool Invocation Patterns](#91-tool-invocation-patterns) for complete list.

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

See [10.1 No Shortcuts Policy](#101-no-shortcuts-policy) for detailed examples.

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

See [10. Common Pitfalls & Troubleshooting](#10-common-pitfalls--troubleshooting) for complete list.

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
├── .github/
│   ├── workflows/
│   │   ├── ci.yml                    # Continuous Integration (lives at repo root /.github/)
│   │   └── code-review.yml          # AI code review (lives at repo root /.github/)
│   └── CODEOWNERS
├── docs/
│   ├── skills/                       # Development skills & guidelines
│   │   ├── architectural-decisions.md
│   │   ├── comprehensive-pr-review.md
│   │   ├── concurrency.md
│   │   ├── documentation.md
│   │   ├── error-handling.md
│   │   ├── security.md
│   │   ├── stay-green.md
│   │   ├── testing.md
│   │   └── vibe.md
│   └── architecture/                 # Architecture documentation
│       └── ADR/                      # Architecture Decision Records
├── scripts/
│   ├── check-all.sh                  # Run all quality checks
│   ├── test.sh                       # Run test suite
│   ├── lint.sh                       # Run linters and type checkers
│   ├── format.sh                     # Format code
│   └── security.sh                   # Security scanning
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
├── requirements.txt                  # Production dependencies
├── requirements-dev.txt              # Development dependencies
├── README.md                         # Project overview
└── CLAUDE.md                         # This file
```

### 5.3 Key Architectural Decisions

All significant architectural decisions are documented in Architecture Decision Records (ADRs) located in `docs/architecture/ADR/`. See [docs/skills/architectural-decisions.md](docs/skills/architectural-decisions.md) for guidelines on creating ADRs.

---

## 6. Quality Standards

### 6.1 Code Quality Requirements

All code must meet these standards before merging to main:

#### Test Coverage
- **Code Coverage**: 90% minimum (branch coverage)
- **Docstring Coverage**: 95% minimum (interrogate)
- **Test Types**: Unit, Integration, and E2E coverage required

#### Type Checking
- **MyPy**: Strict mode, no `# type: ignore` without justification
- **Type Hints**: All function parameters and return types required
- **Generic Types**: Use for collections (List, Dict, etc.)

#### Code Complexity
- **Cyclomatic Complexity**: Max 10 per function
- **Maintainability Index**: Minimum 20 (radon)
- **Max Arguments**: 5 per function
- **Max Branches**: 12 per function
- **Max Lines per Function**: 50 lines

#### Linting and Formatting
- **Ruff**: ALL rules enabled (no exceptions unless documented)
- **Black**: Line length 88 characters
- **isort**: Import sorting per configuration
- **Pylint**: Score of 9.0+ required
- **Bandit**: Security scanning with zero exceptions
- **pip-audit**: Dependency vulnerability checking

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