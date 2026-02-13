---
name: stay-green
description: >-
  2-gate TDD development workflow: Gate 1 is Red-Green-Refactor testing,
  Gate 2 is pre-commit quality checks. Use when implementing features,
  fixing bugs, or doing any development work. Ensures code is never
  committed without passing tests and quality checks.
  Do NOT use for bug-specific debugging (use bug-squashing-methodology).
metadata:
  author: Geoff
  version: 1.0.0
---

# Stay Green

Write tests first, then code. Never declare work finished until all checks pass.

## Instructions

### Gate 1: TDD (Red-Green-Refactor)

1. **Red** - Write a failing test describing the behavior you want
   ```bash
   ./scripts/test.sh --all  # Should fail
   ```

2. **Green** - Write just enough code to make the test pass
   ```bash
   ./scripts/test.sh --all  # Should pass
   ```

3. **Refactor** - Clean up while keeping tests green
   ```bash
   ./scripts/test.sh --all  # Should still pass
   ```

Repeat for each small piece of functionality. Write tests incrementally, not all at once.

### Gate 2: Pre-Commit Quality Checks

```bash
pre-commit run --all-files
```

When checks fail: read errors, fix issues, run again. Repeat until all green.

Quality checks include: formatting (Black + isort), linting (Ruff), type checking (MyPy), complexity (<=10 per function), security (Bandit), tests with coverage (>=90%), file hygiene.

### Work is DONE when:
1. All tests pass (Gate 1 complete)
2. All pre-commit checks pass (Gate 2 complete)

No exceptions.

## Examples

### Example 1: Adding a New Function

```python
# Gate 1 - Red: Write failing test
def test_calculate_cost_from_impressions():
    result = calculate_cost(impressions=1000, cpm=5.0)
    assert result == 5.0

# Gate 1 - Green: Make it pass
def calculate_cost(impressions: int, cpm: float) -> float:
    return impressions * (cpm / 1000)

# Gate 1 - Refactor: (already clean, move on)
# Gate 2: pre-commit run --all-files -> All passed!
```

### Example 2: Fixing a Formatting Failure

```bash
# Gate 2 fails on formatting
$ pre-commit run --all-files
black....Failed

# Auto-fix and re-run
$ ./scripts/format.sh --fix
$ pre-commit run --all-files
# All passed!
```

## Troubleshooting

### Error: Coverage below 90%
```bash
./scripts/test.sh --all --coverage  # See what's not covered
# Add tests for uncovered lines, then re-run pre-commit
```

### Error: Complexity above 10
```bash
./scripts/complexity.sh  # Find complex functions
# Extract helper functions, simplify branching
# Then verify: ./scripts/complexity.sh && pre-commit run --all-files
```

### Error: Type errors from MyPy
```bash
./scripts/typecheck.sh  # See specific errors
# Add/fix type annotations
# Then verify: ./scripts/typecheck.sh && pre-commit run --all-files
```
