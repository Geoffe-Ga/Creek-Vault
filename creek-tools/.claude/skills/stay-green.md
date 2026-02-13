# Stay Green Skill

**Skill Name**: `stay-green`
**Purpose**: Guide development through TDD and quality gates
**Philosophy**: Write tests first, then code. Never declare work finished until all checks pass.

---

## The 2-Gate Workflow

All development follows this simple, iterative workflow:

### Gate 1: TDD (Test-Driven Development) 🔴 ➜ 🟢 ➜ ♻️

**Red-Green-Refactor Loop:**

1. **🔴 Red** - Write a failing test
   - Test describes the behavior you want
   - Test fails because code doesn't exist yet
   - Run: `./scripts/test.sh --all`

2. **🟢 Green** - Write minimal code to pass
   - Write just enough code to make the test pass
   - Don't worry about perfection yet
   - Run: `./scripts/test.sh --all`

3. **♻️ Refactor** - Clean up the code
   - Improve structure while keeping tests green
   - Extract functions, simplify logic
   - Run: `./scripts/test.sh --all` after each change

**Repeat** this loop for each small piece of functionality. Write tests incrementally, not all at once.

---

### Gate 2: Pre-Commit Quality Checks ✅

Before declaring any work finished, iterate on pre-commit until all checks pass:

```bash
pre-commit run --all-files
```

**When checks fail:**
1. Read the error messages
2. Fix the issues (many are auto-fixable)
3. Run `pre-commit run --all-files` again
4. Repeat until **all checks are green**

**Quality checks include:**
- ✓ Code formatting (Black + isort)
- ✓ Linting (Ruff)
- ✓ Type checking (MyPy)
- ✓ Complexity analysis (≤10 per function)
- ✓ Security scanning (Bandit)
- ✓ Tests with coverage (≥90%)
- ✓ File hygiene (trailing whitespace, EOF, etc.)

---

## Complete Workflow Example

```python
# Pseudocode for the Stay Green workflow

def implement_feature():
    """
    Gate 1: TDD Loop
    """
    while feature_not_complete:
        # RED: Write failing test
        write_test_for_next_behavior()
        run_tests()  # Should fail

        # GREEN: Make it pass
        write_minimal_code()
        run_tests()  # Should pass

        # REFACTOR: Clean up
        improve_code_structure()
        run_tests()  # Should still pass

    """
    Gate 2: Quality Checks
    """
    while not all_checks_passing():
        run("pre-commit run --all-files")

        if checks_failed():
            fix_issues()
            # Repeat until green

    """
    Work is DONE ✅
    """
    commit_changes()
```

---

## Common Fixes

### Auto-Fix Formatting
```bash
./scripts/format.sh --fix
pre-commit run --all-files
```

### Coverage Below 90%
```bash
# See what's not covered
./scripts/test.sh --all --coverage

# Add tests for uncovered lines
# Then re-run pre-commit
pre-commit run --all-files
```

### Complexity Above 10
```bash
# Check complexity
./scripts/complexity.sh

# Refactor: Extract functions, simplify branching
# Then verify
./scripts/complexity.sh
pre-commit run --all-files
```

### Type Errors
```bash
# Check types
./scripts/typecheck.sh

# Add type annotations, fix errors
# Then verify
./scripts/typecheck.sh
pre-commit run --all-files
```

---

## Anti-Patterns (Never Do This)

❌ **Don't write code before tests**
- Always write the test first
- Let the test drive the implementation

❌ **Don't skip Gate 2**
- Never declare work finished with failing checks
- Never commit with `--no-verify`

❌ **Don't lower quality thresholds to pass**
- If coverage is low, write more tests
- If complexity is high, refactor code
- Don't change thresholds

❌ **Don't write all tests at once**
- TDD is incremental: one test, one implementation, refactor
- Small steps keep you green

❌ **Don't commit commented-out tests**
- Fix broken tests or use `@pytest.mark.skip(reason="Issue #N")`
- Never hide failures

---

## Quality Thresholds

| Check | Minimum | Tool |
|-------|---------|------|
| **Code Coverage** | ≥90% | pytest-cov |
| **Cyclomatic Complexity** | ≤10 per function | radon, xenon |
| **Type Coverage** | 100% (strict mode) | mypy |
| **Code Quality** | All rules passing | ruff |
| **Security** | No issues | bandit |

---

## Helpful Commands

```bash
# Gate 1: TDD
./scripts/test.sh --all              # Run all tests
./scripts/test.sh --unit             # Unit tests only
./scripts/test.sh --all --coverage   # With coverage report

# Gate 2: Quality checks
pre-commit run --all-files           # Run all checks
./scripts/format.sh --fix            # Auto-fix formatting
./scripts/lint.sh                    # Run linter
./scripts/typecheck.sh               # Type checking
./scripts/complexity.sh              # Check complexity
./scripts/security.sh                # Security scan

# Individual check scripts (for debugging)
./scripts/format.sh --check          # Check formatting only
./scripts/test.sh --unit --coverage  # Unit tests with coverage
```

---

## The Golden Rule

**Work is ONLY finished when:**
1. ✅ All tests pass (Gate 1: TDD complete)
2. ✅ All pre-commit checks pass (Gate 2: Quality verified)

**No exceptions. Stay Green! 🟢**
