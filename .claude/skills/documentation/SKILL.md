---
name: documentation
description: >-
  Write clear, comprehensive documentation with language-specific patterns.
  Use when writing docstrings, module docs, README files, API references,
  or architecture decision records. Covers Python, TypeScript, Go, and Rust
  documentation conventions. Do NOT use for code style (use vibe skill).
metadata:
  author: Geoff
  version: 1.0.0
---

# Documentation

Create clear, comprehensive documentation that serves as a reliable reference for both humans and AI agents.

## Instructions

### Step 1: Follow the Principles

1. Write for your future self (you will forget)
2. Document the "why", not just the "what"
3. Keep documentation close to code
4. Use examples liberally
5. Update docs when changing code
6. Make documentation searchable and navigable

### Step 2: Apply Language-Specific Patterns

See `references/` for detailed patterns:
- `references/python-patterns.md` - Google-style docstrings, module docs
- `references/typescript-patterns.md` - TSDoc comments, README sections
- `references/go-patterns.md` - Package and function documentation
- `references/rust-patterns.md` - Doc comments with examples

### Step 3: Verify Completeness

Use the documentation checklist:
- [ ] All public functions have docstrings
- [ ] All public classes have class docstrings
- [ ] Module has module-level docstring
- [ ] All parameters documented
- [ ] Return values documented
- [ ] All exceptions documented
- [ ] At least one example provided
- [ ] Complex logic has inline comments
- [ ] Error messages are clear and actionable

## Examples

### Example 1: Python Function Documentation

```python
def process_file(
    input_path: Path,
    output_path: Path,
    *,
    encoding: str = "utf-8",
    validate: bool = True,
) -> dict[str, int]:
    """Process input file and write results to output file.

    Args:
        input_path: Path to input file. Must exist and be readable.
        output_path: Path to output file. Parent directory must exist.
        encoding: Character encoding for file I/O. Defaults to UTF-8.
        validate: Whether to validate input before processing.

    Returns:
        Dictionary with 'lines_processed', 'tokens_found', 'errors_encountered'.

    Raises:
        FileNotFoundError: If input_path does not exist.
        ValueError: If validate=True and input content is invalid.

    Examples:
        >>> result = process_file(Path("input.txt"), Path("output.txt"))
        >>> print(f"Processed {result['lines_processed']} lines")
    """
```

### Example 2: Anti-Pattern vs Good Documentation

**Bad** - documents implementation:
```python
def fetch_user(user_id: str) -> User:
    """First we check the cache using a dict lookup.
    If not found, we make an HTTP GET request to /api/users/{id}.
    Then we parse the JSON response using json.loads()."""
```

**Good** - documents interface:
```python
def fetch_user(user_id: str) -> User:
    """Retrieve user by ID from API.

    Fetches user data from the API, using cache when available.

    Args:
        user_id: Unique user identifier

    Returns:
        User object with profile data

    Raises:
        UserNotFoundError: If user doesn't exist

    Note:
        Results are cached for 5 minutes.
    """
```

## Troubleshooting

### Error: Documentation gets stale after code changes
- Update docs in the same commit as code changes
- Add docstring checks to CI (e.g., interrogate for Python)
- Review docstrings during PR review

### Error: Documentation is too verbose
- Focus on the "what" and "why", not the "how"
- Use type hints to reduce parameter description length
- Link to detailed examples instead of inlining them
