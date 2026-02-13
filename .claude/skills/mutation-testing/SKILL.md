---
name: mutation-testing
description: >-
  Write high-value tests that kill mutants through logic validation.
  Use when writing tests that need to verify behavior, improving
  mutation scores, or evaluating test quality. Covers boundary testing,
  exact value assertions, error message validation, and state verification.
  Do NOT use for general test writing (use testing skill).
metadata:
  author: Geoff
  version: 1.0.0
---

# Mutation Testing

Mutation testing reveals whether your tests verify behavior or just achieve coverage. A test that doesn't kill mutants doesn't test.

## Instructions

### Step 1: Apply the Mutant-Killing Checklist

For every test, ask:

1. **Would this fail if I changed a constant by 1?** -> Test exact values
2. **Would this fail if I changed an operator?** -> Test exact boundaries
3. **Would this fail if I removed a check?** -> Test that validation happens
4. **Would this fail if I changed an error message?** -> Validate message content

### Step 2: Use High-Value Test Patterns

- **Exact constants**: `assert MAX_RETRIES == 3` (not just `assert MAX_RETRIES > 0`)
- **Boundary testing**: Test at, above, and below the boundary
- **Complete error messages**: Match on message content, not just exception type
- **State verification**: Assert before and after state changes
- **Collection contents**: Test exact contents and order, not just size

### Step 3: Write Testable Code

- Use named constants for magic numbers
- Write specific error messages
- Make behavior testable (raise exceptions, don't just print)

### Step 4: Run Mutation Tests

```bash
./scripts/mutation.sh --paths-to-mutate src/module.py
./scripts/analyze_mutations.py
# Target: >= 80% mutation score
```

## Examples

See `references/practical-examples.md` for complete worked examples.

### Example 1: Boundary Testing

```python
# BAD: Doesn't kill MAX_LENGTH = 100 -> 101
assert len(name) <= MAX_PROJECT_NAME_LENGTH

# GOOD: Kills the mutant
assert MAX_PROJECT_NAME_LENGTH == 100
assert is_valid("a" * 100) == True   # At max
assert is_valid("a" * 101) == False  # Over max
assert is_valid("a" * 99) == True    # Under max
```

### Example 2: Error Message Validation

```python
# BAD: Doesn't kill message mutations
with pytest.raises(FileNotFoundError):
    load_config(missing_file)

# GOOD: Validates exact message
with pytest.raises(FileNotFoundError) as exc:
    load_config(missing_file)
assert "Configuration file not found" in str(exc.value)
assert str(missing_file) in str(exc.value)
```

## Troubleshooting

### Error: Mutation score below 80%
- Run `mutmut show <id>` to see surviving mutants
- Focus on logic mutations (constants, operators, conditions) first
- Add exact value assertions, not just truthiness checks
- Test both branches of every condition

### Error: Too many equivalent mutants
- Some mutations produce equivalent behavior and can't be killed
- Focus on mutations that actually change observable behavior
- Use `mutmut show` to identify which are truly equivalent
