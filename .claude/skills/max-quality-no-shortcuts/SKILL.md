---
name: max-quality-no-shortcuts
description: >-
  Anti-bypass philosophy for linter and type checker warnings. Activates
  when you consider adding noqa, type-ignore, pylint-disable, or similar
  bypasses. Fix the root cause instead. Covers complexity refactoring,
  type fixes, and argument reduction patterns.
  Do NOT use for general code quality guidance (use vibe or stay-green).
metadata:
  author: Geoff
  version: 1.0.0
---

# MAX QUALITY: No Shortcuts

When you're about to add a linter bypass: STOP. Fix the root cause instead.

## Instructions

### Step 1: Understand the Warning

Read the error message carefully. Look up the rule if unfamiliar. Understand the underlying principle the tool is enforcing.

### Step 2: Identify the Root Cause

Ask:
- Is my code too complex? -> Refactor into smaller functions
- Is my type annotation wrong? -> Fix the type or implementation
- Is my import unused? -> Remove it
- Is my function too long? -> Extract helper functions
- Are there too many arguments? -> Use a dataclass/config object

### Step 3: Fix Properly

See `references/fix-examples.md` for detailed before/after examples.

| Bypass | Proper Fix |
|--------|-----------|
| `# noqa: C901` (complexity) | Refactor into smaller functions |
| `# type: ignore` | Fix the type annotation or implementation |
| `# pylint: disable=invalid-name` | Use proper naming conventions |
| `# noqa: F401` (unused import) | Remove the import |
| `# pylint: disable=too-many-arguments` | Use a config object or dataclass |
| `# noqa: E501` (line too long) | Break into multiple lines |

### Step 4: Handle Genuine Exceptions

Bypasses are acceptable ONLY for:
1. Third-party library bugs you can't fix
2. Python version compatibility
3. Benchmarked performance necessity
4. Auto-generated code you don't control

For these cases, you MUST include:
```python
# pylint: disable=no-member
# Reason: boto3 dynamically creates methods via __getattr__
# Reference: https://github.com/boto/boto3/issues/123
# Alternative considered: Custom wrapper (unnecessary complexity)
# Review: 2026-06-27
```

## Examples

See `references/fix-examples.md` for complete refactoring examples.

### Example 1: Complexity Too High

```python
# BAD: # noqa: C901
def process_order(order, user, payment, shipping):
    if order.status == "pending":
        if user.is_verified:
            if payment.is_valid:
                # 30 more lines of nested ifs...

# GOOD: Extract validation functions
def process_order(order, user, payment, shipping) -> Result:
    _validate_order_ready(order)
    _validate_user_eligible(user)
    _validate_payment(payment)
    _validate_shipping(shipping)
    return _fulfill_order(order, user, payment, shipping)
```

### Example 2: Type Error

```python
# BAD: return config.get(key)  # type: ignore[return-value]

# GOOD: Fix the return type
def get_config(key: str) -> str | None:
    return config.get(key)

# Or if the key must exist:
def get_required_config(key: str) -> str:
    value = config.get(key)
    if value is None:
        raise ConfigError(f"Required key not found: {key}")
    return value
```

## Troubleshooting

### Error: "I genuinely can't fix this without a bypass"
- Is it a third-party library issue? Document it with a link to the issue tracker
- Is it a Python version limitation? Document which versions are affected
- Can you restructure the code to avoid the situation entirely?
- If truly necessary, add full justification comment with review date

### Error: "Fixing this properly will take too long"
- 10 minutes fixing properly now saves 2 hours debugging later
- The bypass will still be there when you come back
- Each bypass makes the next one easier to justify
