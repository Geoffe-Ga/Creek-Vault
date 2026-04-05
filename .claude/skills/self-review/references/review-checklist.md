# Self-Review Detailed Checklist

Use this checklist as a systematic sweep. Check every item for every changed file.

## Bug Checklist

- [ ] No off-by-one errors in loops, slices, or ranges
- [ ] No None/null dereference without guard
- [ ] No unhandled exceptions from external calls (file I/O, network, parsing)
- [ ] No race conditions in shared state
- [ ] No infinite loops or unbounded recursion
- [ ] All match/case statements have default/fallback
- [ ] All dictionary accesses use .get() or guard with `in`
- [ ] Return values are used (not silently discarded)
- [ ] Mutable default arguments avoided (no `def f(x=[])`)
- [ ] String formatting uses f-strings or .format(), not % (Python)

## Architecture Checklist

- [ ] Each class has a single, clear responsibility
- [ ] Public API is minimal (private by default)
- [ ] No circular imports
- [ ] Dependency direction flows inward (domain doesn't depend on infrastructure)
- [ ] No God objects (classes with 10+ methods or 500+ lines)
- [ ] Configuration is injected, not hardcoded
- [ ] Follows existing patterns in the codebase (check nearby modules)

## Security Checklist (OWASP-informed)

- [ ] No user input reaches shell commands (subprocess, os.system)
- [ ] No user input in file paths without sanitization
- [ ] No eval(), exec(), or dynamic code generation with user input
- [ ] yaml.safe_load() used (never yaml.load())
- [ ] json.loads() used for untrusted JSON (not eval)
- [ ] No hardcoded secrets, tokens, or passwords
- [ ] File permissions are restrictive (not world-readable for sensitive files)
- [ ] New dependencies checked for known CVEs

## Ethics Checklist (Creek-specific)

- [ ] User's personal data stays local (no cloud calls without opt-in)
- [ ] Redaction runs before any data leaves the local system
- [ ] User can delete/purge any data the system creates
- [ ] No hidden data collection or telemetry
- [ ] Error messages don't leak sensitive content
- [ ] Failure modes protect the user (fail closed, not open)
- [ ] Content classification doesn't make value judgments about the user

## Testing Checklist

- [ ] Every public function has at least one test
- [ ] Tests assert specific expected values (not just "no exception")
- [ ] Edge cases tested: empty, None, zero, boundary, unicode
- [ ] Error paths tested: invalid input, missing files, malformed data
- [ ] Tests are independent (no shared mutable state between tests)
- [ ] Test names describe the behavior: `test_scanner_skips_binary_files`
- [ ] No flaky tests (time-dependent, order-dependent, network-dependent)
- [ ] Mocks are minimal (mock boundaries, not internals)

## Creek Project Compliance

- [ ] All new functions have type annotations
- [ ] All public functions have docstrings
- [ ] No function exceeds cyclomatic complexity of 10
- [ ] No linter bypasses without full justification
- [ ] Conventional commit message format
- [ ] Tests achieve >= 90% branch coverage on new code
- [ ] New config options added to CreekConfig if applicable
