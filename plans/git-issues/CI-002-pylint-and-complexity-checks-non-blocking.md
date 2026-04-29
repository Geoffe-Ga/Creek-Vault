# CI-002: Pylint and Xenon complexity gates are non-blocking in CI

**Severity:** Medium
**Category:** CI
**Estimated complexity:** S (≤2h)
**Parallelizable with peers in same category:** yes
**Discovered by:** Reading `.github/workflows/ci.yml`

## Files affected
- `.github/workflows/ci.yml:77-82` (pylint), `218-222` (xenon)
- `creek-tools/CLAUDE.md` §6.1 — claim "Pylint Score: ≥9.0", "Cyclomatic Complexity: Max 10 per function"

## Dependencies
None.

## Reproduction
```yaml
- name: Run Pylint
  run: |
    pylint **/*.py --output-format=colorized --fail-under=8.0 || true
    pylint **/*.py --output-format=json > reports/pylint-report.json || true
  continue-on-error: true
```

Both invocations end with `|| true`, and the step is `continue-on-error: true`. Pylint cannot fail the build.

```yaml
- name: Check complexity thresholds
  run: |
    xenon --max-absolute B --max-modules B --max-average A . || true
  continue-on-error: true
```

Same shape. Xenon gate cannot fail the build.

Also: `--fail-under=8.0` in CI vs claimed "Pylint Score: ≥9.0". Even if the gate ran, the threshold is wrong.

## Analysis

Two gates the project advertises as enforced are not. The fix is identical to CI-001: drop `|| true` and `continue-on-error: true`, fix the threshold, prefer running the project's own scripts (`scripts/complexity.sh`).

A wrinkle: `scripts/complexity.sh` itself uses `radon cc … || true` for the human-readable output but only the `xenon` line is supposed to be enforcing. So the script is consistent — but the script then *exits 0* even if xenon hadn't been installed. Tighten that too.

Confidence: verified.

## Proposed remediation

1. Replace the inline pylint step with `pylint creek/ --fail-under=9.0` — no `|| true`, no `continue-on-error`.
2. Replace the inline xenon step with `./scripts/complexity.sh` and have the script `exit 1` if xenon isn't installed (currently it just prints a note).
3. Either align the pylint threshold to the documented 9.0, or update the doc to 8.0.

## Acceptance criteria

- A function with cyclomatic complexity 12 fails CI.
- A code change that drops the pylint score below the configured threshold fails CI.
- Threshold matches `creek-tools/CLAUDE.md`.
- Local `./scripts/check-all.sh` exhibits the same behaviour as CI.

## References
- `.github/workflows/ci.yml:77-82, 218-222`
- `creek-tools/scripts/complexity.sh`
- `creek-tools/CLAUDE.md` §6.1
