---
name: security
description: >-
  Implement secure coding practices against common vulnerabilities.
  Use when handling user input, file paths, subprocess calls, SQL queries,
  API keys, or building web endpoints. Covers input validation, injection
  prevention, secret management, and XSS/CSRF protection across Python,
  TypeScript, Go, and Rust. Do NOT use for general error handling
  (use error-handling skill).
metadata:
  author: Geoff
  version: 1.0.0
---

# Security

Defense in depth, least privilege, fail securely, never trust user input.

## Instructions

### Step 1: Follow the Principles

1. Defense in depth - multiple layers of security
2. Least privilege - minimal access required
3. Fail securely - errors don't expose sensitive data
4. Input validation at all boundaries
5. Never trust user input
6. Secure by default, not by configuration

### Step 2: Apply Security Patterns

See `references/patterns-by-language.md` for language-specific implementations.

Key areas:
- **Input validation**: Allowlist characters, validate format, limit length
- **Path traversal prevention**: Resolve paths, verify within base directory
- **Command injection prevention**: Never use `shell=True`, validate arguments
- **SQL injection prevention**: Always use parameterized queries
- **XSS prevention**: Escape HTML output, use framework defaults
- **Secret management**: Use OS keyring, never hardcode secrets
- **CSRF protection**: Use tokens with constant-time comparison

### Step 3: Run the Security Checklist

Before deploying:
- [ ] All user inputs validated at boundaries
- [ ] No SQL injection (parameterized queries)
- [ ] No command injection (no `shell=True`)
- [ ] No path traversal (validated file paths)
- [ ] No XSS (escaped HTML)
- [ ] API keys in OS keyring, not code
- [ ] Secrets not committed to git
- [ ] HTTPS for all external requests
- [ ] SSL certificates validated
- [ ] Error messages don't leak sensitive data
- [ ] Logging doesn't include secrets or PII

## Examples

See `references/examples.md` for complete worked examples.

### Example 1: Safe Path Handling (Python)

```python
def safe_path_join(base_dir: Path, user_path: str) -> Path:
    base_dir = base_dir.resolve()
    target = (base_dir / user_path).resolve()
    try:
        target.relative_to(base_dir)
    except ValueError:
        raise ValueError(f"Path traversal detected: {user_path!r}") from None
    return target
```

### Example 2: Safe Subprocess Call (Python)

```python
# WRONG - shell injection vulnerability
subprocess.run(f"git clone {user_repo}", shell=True)

# CORRECT - no injection possible
subprocess.run(["git", "clone", user_repo], check=True)
```

## Troubleshooting

### Error: Keyring not available in CI/headless environment
- Use environment variables as fallback for CI only
- Document the fallback in CI configuration
- Never use environment variables for production secret storage

### Error: Path validation too restrictive
- Check if you're resolving symlinks unnecessarily
- Verify the base directory itself is resolved before comparison
- Test with both relative and absolute user paths
