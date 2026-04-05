---
name: self-review
description: >-
  Rigorous self-review of your own code changes before PR creation. Use when
  you have finished implementing and want to review your own diff, when asked
  to "review my changes", "self-review", "check my work", or "pre-PR review".
  Covers bugs, architecture, security, ethics, testing, and project compliance.
  Do NOT use for reviewing others' PRs (use comprehensive-pr-review),
  CI failures (use ci-debugging), or backlog triage (use backlog-grooming).
metadata:
  author: Geoff
  version: 1.0.0
---

# Self-Review

Review your own changes with the same rigor you would apply to a colleague's PR. Find and fix issues before anyone else sees the code.

## Instructions

### Step 1: Gather the Diff

```bash
# Full diff against the base branch
git diff main...HEAD

# Files changed
git diff main...HEAD --name-only

# Stat summary
git diff main...HEAD --stat
```

Read every changed file in full context (not just the diff). Understand how your changes interact with surrounding code.

### Step 2: Bug Audit

For each changed function, verify:
- **Correctness**: Does it do what the issue/docstring says?
- **Edge cases**: Empty input, None, zero, negative, very large, unicode, concurrent access
- **Off-by-one**: Loop boundaries, slicing, pagination, indexing
- **Error paths**: What happens when dependencies fail? Missing files? Network errors?
- **Resource leaks**: Are files/connections closed? Context managers used?
- **State mutation**: Does it modify shared state unexpectedly?

Flag with severity:
- **BLOCKING**: Will cause runtime errors, data loss, or incorrect results
- **HIGH**: Likely to cause problems under realistic conditions
- **LOW**: Theoretical or minor concern

### Step 3: Architecture Review

Evaluate structural decisions:
- **Single Responsibility**: Does each class/function do one thing?
- **Interface design**: Are public APIs intuitive? Would another developer understand them?
- **Coupling**: Do your changes create tight coupling to implementation details?
- **Naming**: Do names accurately describe behavior? Would a stranger understand?
- **Abstractions**: Are you abstracting at the right level? (Not too early, not too late)
- **Consistency**: Do your patterns match the existing codebase conventions?

### Step 4: Security Review

Check for vulnerabilities (OWASP-informed):
- **Injection**: SQL, command, path traversal, template injection
- **Input validation**: Are all external inputs validated at system boundaries?
- **Secrets**: No hardcoded credentials, API keys, tokens, or connection strings
- **File operations**: Path traversal prevented? Symlink attacks considered?
- **Deserialization**: Safe loading (yaml.safe_load, not yaml.load)?
- **Dependencies**: Any new dependencies with known vulnerabilities?
- **Permissions**: Principle of least privilege followed?

Flag security issues as BLOCKING. Never merge code with known security vulnerabilities.

### Step 5: Ethics Review

Consider the human impact:
- **Privacy**: Does this handle personal data? Is it minimized, encrypted, redactable?
- **Consent**: Does the user know what data is collected and processed?
- **Bias**: Could this produce unfair or discriminatory outcomes?
- **Transparency**: Can the user understand what the system did and why?
- **Reversibility**: Can actions be undone? Is there a purge/delete path?
- **Failure modes**: When this fails, does it fail safely? Does it protect the user?

For the Creek project specifically: content enters a personal knowledge vault. Respect the user's relationship with their own data.

### Step 6: Testing Review

Verify test quality:
- **Coverage**: Do tests cover all new code paths? (`./scripts/coverage.sh`)
- **Assertions**: Tests assert specific values, not just "no crash"
- **Edge cases**: Boundary conditions, empty inputs, error paths tested
- **Independence**: Tests don't depend on execution order or shared state
- **Naming**: Test names describe the behavior being verified
- **No false confidence**: Would these tests catch a bug if someone broke the code?

Ask: "If I mutated each conditional, would a test fail?" If not, the test is weak.

### Step 7: Project Compliance

Verify against creek-tools standards:
- [ ] Test coverage >= 90% (branch): `./scripts/coverage.sh`
- [ ] Docstring coverage >= 95%: check interrogate output
- [ ] Cyclomatic complexity <= 10: `./scripts/complexity.sh`
- [ ] MyPy strict mode passes: `./scripts/typecheck.sh`
- [ ] Ruff zero violations: `./scripts/lint.sh`
- [ ] Bandit zero findings: `./scripts/security.sh`
- [ ] Conventional commit message format used
- [ ] No linter bypasses without full justification (max-quality-no-shortcuts)

### Step 8: Fix and Verify

For each finding:
1. Fix BLOCKING issues immediately
2. Fix HIGH issues before PR
3. Document LOW issues as TODO comments (with issue number if deferring)
4. Re-run quality gates after every fix:
   ```bash
   cd creek-tools && ./scripts/check-all.sh
   ```

### Step 9: Deliver Verdict

Produce a summary:

```markdown
## Self-Review: [branch-name]

### Findings
- BLOCKING: [count] (all resolved)
- HIGH: [count] (all resolved)
- LOW: [count] ([N] deferred with TODOs)

### Sections
- Bugs: [PASS/findings]
- Architecture: [PASS/findings]
- Security: [PASS/findings]
- Ethics: [PASS/findings]
- Testing: [PASS/findings]
- Compliance: [PASS/findings]

### Verdict: [PASS / NEEDS WORK]

### Quality Gates
- check-all.sh: [PASS]
- pre-commit: [PASS]
```

Verdict is PASS only when: zero BLOCKING, zero HIGH, all quality gates green.

## Examples

### Example 1: Clean Pass

```markdown
## Self-Review: feat/28-embedding-generation

### Findings
- BLOCKING: 0
- HIGH: 0
- LOW: 1 (deferred: consider caching embeddings to disk)

### Sections
- Bugs: PASS
- Architecture: PASS (follows existing linker pattern)
- Security: PASS (no external input, no file I/O beyond vault)
- Ethics: PASS (embeddings are local-only, no data leaves machine)
- Testing: PASS (94% coverage, edge cases for empty/short fragments)
- Compliance: PASS (all thresholds met)

### Verdict: PASS

### Quality Gates
- check-all.sh: PASS (exit 0)
- pre-commit: PASS (all hooks green)
```

### Example 2: Issues Found and Fixed

```markdown
## Self-Review: feat/14-redaction-scanner

### Findings
- BLOCKING: 1 (FIXED: scanner.py:89 path traversal via user-supplied exclusion pattern)
- HIGH: 2 (FIXED: missing binary file detection for .pdf, missing tqdm dependency in pyproject.toml)
- LOW: 1 (deferred: tqdm could be optional dependency)

### Sections
- Bugs: PASS (after fix)
- Architecture: PASS
- Security: PASS (after path traversal fix — now validates patterns against allowlist)
- Ethics: PASS (redaction protects user privacy by design)
- Testing: PASS (91% coverage, added path traversal test)
- Compliance: PASS

### Verdict: PASS (after 3 fixes)

### Quality Gates
- check-all.sh: PASS (exit 0)
- pre-commit: PASS (all hooks green)
```

## Troubleshooting

### Error: Too many findings to fix in one pass
Prioritize: BLOCKING first, then HIGH. If LOW findings are numerous, batch them into a follow-up issue rather than scope-creeping the current PR.

### Error: Fixing a finding breaks tests
You introduced a regression. Go back to the TDD cycle (stay-green): write a test for the correct behavior, fix the code, verify all tests pass.

### Error: Not sure if something is BLOCKING or LOW
Ask: "If this shipped to production, would it cause data loss, security exposure, or incorrect behavior?" If yes, BLOCKING. If "maybe under unusual conditions", HIGH. If "cosmetic or theoretical", LOW.

### Error: Ethics review feels subjective
Anchor to specifics: What data is collected? Who can see it? Can the user delete it? Does it fail open or closed? If you can't answer these for your code, that's a finding.
