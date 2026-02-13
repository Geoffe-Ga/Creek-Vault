# Comprehensive PR Review Skill

## Purpose
Provide structured, thorough code reviews that evaluate PRs across multiple dimensions: security, quality, testing, documentation, and project compliance.

## Review Structure

### 1. Summary
Brief overview of what the PR does (2-3 sentences)

### 2. Strengths
What is done well:
- Good design decisions
- Well-written code
- Comprehensive tests
- Clear documentation
- Proper error handling

### 3. Security Concerns
Critical security issues:
- 🔴 **BLOCKING**: Must fix before merge
- 🟡 **HIGH**: Should fix soon
- 🟢 **LOW**: Nice to have

### 4. Problems
Critical issues blocking merge:
- Bugs or incorrect logic
- Failing tests
- Missing required features

### 5. Code Quality
Non-blocking improvements:
- Readability
- Naming conventions
- Code organization
- Complexity

### 6. CLAUDE.md Adherence
Check project requirements:
- ✅/❌ 90% test coverage?
- ✅/❌ 95% docstring coverage?
- ✅/❌ Ruff ALL rules passing?
- ✅/❌ MyPy strict mode passing?
- ✅/❌ No forbidden patterns?
- ✅/❌ Conventional commits?

### 7. Testing
- Test coverage adequate?
- Edge cases covered?
- Integration tests included?

### 8. Documentation
- Docstrings complete?
- README updated?
- Examples provided?

### 9. Requests
Medium-priority suggestions

### 10. Verdict
- ✅ **LGTM** - Ready to merge
- 🔄 **CHANGES_REQUESTED** - Must fix blocking issues
- 💬 **COMMENTS** - Suggestions only

**Reasoning:** Explain verdict with specific references
