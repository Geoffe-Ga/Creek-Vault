---
name: error-handling
description: >-
  Implement robust error handling that fails fast with clear diagnostics.
  Use when designing error strategies, writing exception handling,
  creating custom error types, or implementing validation. Covers
  Python, TypeScript, Go, and Rust patterns.
  Do NOT use for security-specific input validation (use security skill).
metadata:
  author: Geoff
  version: 1.0.0
---

# Error Handling

Fail fast with clear error messages. Use typed exceptions, include context, never silence errors.

## Instructions

### Step 1: Follow the Principles

1. Fail fast with clear error messages
2. Use typed exceptions/errors over generic ones
3. Include context in error messages (what, where, why)
4. Never silence errors without logging
5. Validate inputs at boundaries
6. Handle errors at the appropriate abstraction level

### Step 2: Apply Language-Specific Patterns

See `references/patterns-by-language.md` for detailed patterns in Python, TypeScript, Go, and Rust.

Key patterns:
- **Python**: Typed exceptions with context, context managers for cleanup, specific exception catching
- **TypeScript**: Custom error classes, Result type pattern, Promise error handling
- **Go**: Error wrapping with `fmt.Errorf` and `%w`, custom error types, immediate error checking
- **Rust**: Custom error enums with `Display`, `?` operator for propagation, `map_err` for context

### Step 3: Avoid Anti-Patterns

- **Silent failures**: `except: pass` - always log or re-raise
- **Generic catching**: `except Exception` - catch specific exceptions
- **Bare except**: Catches KeyboardInterrupt and SystemExit
- **No context**: `raise ValueError("Invalid")` - include what, where, why
- **Error codes**: Return -1 on error - use exceptions/Result types

## Examples

See `references/examples.md` for complete worked examples.

### Example 1: Python - Typed Exception with Context

```python
class ConfigurationError(Exception):
    def __init__(self, field: str, value: str, reason: str) -> None:
        self.field = field
        self.value = value
        super().__init__(f"Invalid {field}={value!r}: {reason}")

# Usage
raise ConfigurationError(
    field="timeout",
    value="abc",
    reason="Must be a positive integer",
)
```

### Example 2: TypeScript - Result Type Pattern

```typescript
type Result<T, E = Error> =
  | { ok: true; value: T }
  | { ok: false; error: E };

function parseConfig(content: string): Result<Config> {
  try {
    const data = JSON.parse(content);
    if (!isValidConfig(data)) {
      return { ok: false, error: new Error('Invalid config structure') };
    }
    return { ok: true, value: data };
  } catch (error) {
    return { ok: false, error: error instanceof Error ? error : new Error('Unknown error') };
  }
}
```

## Troubleshooting

### Error: Too many exception types making code complex
- Create a base exception for your module, subclass for specific cases
- Use 3-5 exception types per module, not one per function
- Group by category: ValidationError, NotFoundError, PermissionError

### Error: Error messages aren't helpful for debugging
- Include the failing value: `f"Invalid email: {email!r}"`
- Include the location: `f"in {path.absolute()}`
- Include the expectation: `f"expected positive integer, got {value}"`
