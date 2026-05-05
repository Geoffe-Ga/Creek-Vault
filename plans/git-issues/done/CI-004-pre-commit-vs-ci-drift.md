# CI-004: Pre-commit and CI run different toolsets and disagree on enforcement

**Severity:** Medium
**Category:** CI
**Estimated complexity:** S (≤2h)
**Parallelizable with peers in same category:** yes
**Discovered by:** Dimension 10

## Files affected
- `creek-tools/.pre-commit-config.yaml`
- `.github/workflows/ci.yml`
- `creek-tools/scripts/check-all.sh`

## Dependencies
CI-001 (mypy strictness), CI-002 (pylint/xenon enforcement).

## Reproduction
Pre-commit runs (per `.pre-commit-config.yaml`):
- ruff (lint + format)
- mypy `--strict`
- bandit
- shellcheck
- pyupgrade
- autoflake
- tryceratops
- refurb
- vulture
- interrogate
- detect-secrets

CI runs:
- ruff (check + format)
- mypy `--ignore-missing-imports --no-strict-optional` (with `|| true` and `continue-on-error: true`)
- pylint (`continue-on-error: true`)
- bandit
- pip-audit
- interrogate
- xenon (`|| true`, `continue-on-error: true`)

Tools in pre-commit but not CI: shellcheck, pyupgrade, autoflake, tryceratops, refurb, vulture, detect-secrets.
Tools in CI but not pre-commit: pylint, pip-audit.

Strictness mismatches: mypy (CI is permissive), several CI gates have `continue-on-error`.

## Analysis

`creek-tools/CLAUDE.md` §1.1 says "Always invoke tools through `./scripts/*` instead of directly. Why: Scripts ensure consistent configuration across local development and CI."

The reality: scripts call one toolset; pre-commit calls another; CI calls a third. A change that passes pre-commit on the developer's machine may fail (or silently pass-with-warnings) in CI. A change that passes CI may fail pre-commit on someone else's machine.

For the launch readiness of a project that promises "MAXIMUM QUALITY," this drift is itself a quality issue.

## Proposed remediation

Single source of truth: `creek-tools/scripts/check-all.sh`. Have:
- Pre-commit invoke the same script (or sub-scripts) so a hook failure means a CI failure.
- CI invoke `./scripts/check-all.sh` directly. Drop the inline `pylint`, `mypy`, `xenon`, `bandit`, etc. invocations in `ci.yml`.

This shrinks the workflow file dramatically and removes the "which version of mypy is running?" confusion.

Move tools that should be in CI but aren't (refurb, tryceratops, vulture, shellcheck, detect-secrets) into `check-all.sh` so CI gets them automatically.

## Acceptance criteria

- `./scripts/check-all.sh` exit code matches the CI quality gate result for any commit.
- Every tool listed in `.pre-commit-config.yaml` is also called by `check-all.sh`.
- Removing a hook from pre-commit removes it from CI in the same change.
- Pre-commit and CI run mypy with the same flags.

## References
- `creek-tools/.pre-commit-config.yaml`
- `.github/workflows/ci.yml`
- `creek-tools/scripts/check-all.sh`
- `creek-tools/CLAUDE.md` §1.1
- CI-001, CI-002, STYLE-001
