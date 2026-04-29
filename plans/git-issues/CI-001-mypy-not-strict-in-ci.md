# CI-001: CI runs MyPy with `--ignore-missing-imports --no-strict-optional` and `continue-on-error: true`

**Severity:** High
**Category:** CI
**Estimated complexity:** S (≤2h)
**Parallelizable with peers in same category:** yes
**Discovered by:** Reading `.github/workflows/ci.yml`

## Files affected
- `.github/workflows/ci.yml:83-86`
- `creek-tools/CLAUDE.md` §6.1 — claim "MyPy: Strict mode, no `# type: ignore` without justification"

## Dependencies
None.

## Blockers
This is the kind of "declared-but-unenforced" gate that the review brief flags as itself an issue. Until fixed, every other type-safety guarantee is on the honour system.

## Reproduction
Read the workflow:
```yaml
- name: Run MyPy type checking
  run: |
    mypy . --ignore-missing-imports --no-strict-optional --show-error-codes --pretty || true
  continue-on-error: true
```

Three layers of permissiveness: `--ignore-missing-imports` and `--no-strict-optional` flatten strictness; `|| true` swallows failures inside the script; `continue-on-error: true` swallows failures at the step level.

Local `creek-tools/scripts/typecheck.sh` correctly runs `mypy creek/` (which respects pyproject.toml's `[tool.mypy] strict = true` overrides). CI does not.

## Analysis

Consequences:
- New code can land that mypy `--strict` would reject.
- Devs running `./scripts/typecheck.sh` locally see errors that CI ignores → "works on CI" frustration when CI greens but local checks are red.
- The CLAUDE.md "Quality Standards" claim is hollow.
- `pylint` has the same shape (`continue-on-error: true`, line 81) — see CI-002.

Confidence: verified — read ci.yml line by line.

## Proposed remediation

```yaml
- name: Run MyPy type checking
  working-directory: creek-tools
  run: ./scripts/typecheck.sh
```

Drop `continue-on-error: true`. Drop the `--ignore-missing-imports --no-strict-optional` flags. Use the project's own script so local and CI agree.

For optional deps that genuinely have no stubs (`anthropic`, `googleapiclient`, etc.), keep the `[[tool.mypy.overrides]] ignore_missing_imports = true` per-module overrides already in `pyproject.toml`. Don't disable strictness globally.

## Acceptance criteria

- CI run with a deliberately-broken type annotation fails the build.
- CI run with current code passes (verify by running the same command locally).
- The workflow uses the project's `typecheck.sh` rather than re-implementing the invocation.
- A regression test (e.g., a one-line `def foo(x): return x.bar` in a module CI checks) demonstrates the gate works.

## References
- `.github/workflows/ci.yml:83-86`
- `creek-tools/scripts/typecheck.sh`
- `creek-tools/CLAUDE.md` §6.1
