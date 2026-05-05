# CI-003: CI test step does not filter on markers — runs integration / e2e in the same matrix as unit

**Severity:** Low
**Category:** CI
**Estimated complexity:** S (≤2h)
**Parallelizable with peers in same category:** yes
**Discovered by:** Reading `.github/workflows/ci.yml`

## Files affected
- `.github/workflows/ci.yml:113-127`
- `creek-tools/scripts/test.sh` — local default is `not integration and not e2e`

## Dependencies
None.

## Reproduction
Compare:
```yaml
# CI
pytest --cov=. ... --cov-fail-under=${{ env.COVERAGE_THRESHOLD }} ...
# (no -m filter)
```

```bash
# Local
./scripts/test.sh           # adds -m "not integration and not e2e"
./scripts/test.sh --all     # runs everything
```

CI runs everything by default. Locally, the dev runs unit only by default. So the matrix is asymmetric.

## Analysis

Two consequences:
1. **Drift.** A test marked `@pytest.mark.integration` that requires a service (Ollama, real network) might pass locally because the user skipped it but fail in CI. Or vice versa: a heavyweight integration test might skip in CI (because the service isn't running) and the team thinks it's covered.
2. **Wall time.** Integration / e2e tests would slow every CI run unnecessarily.

Today there are only 6 integration tests and zero e2e (TEST-001), so the impact is limited — but as the test suite grows, the asymmetry will bite.

## Proposed remediation

Have CI call `./scripts/test.sh --all --coverage` explicitly, with whatever skip conditions the integration tests need (env vars, optional deps). Or split into two jobs: a fast `unit` matrix on 3.11/3.12/3.13 and a slow `integration` job that runs once.

Alternative: have CI mirror the local default — run unit only, mark integration/e2e as opt-in via a separate workflow.

## Acceptance criteria

- Local `./scripts/test.sh` and CI run the same set of tests by default.
- An integration-marked test that requires Ollama doesn't fail CI when Ollama isn't running (it skips with a clear message).
- The workflow explicitly calls a project script rather than re-implementing pytest invocation.

## References
- `.github/workflows/ci.yml:113-127`
- `creek-tools/scripts/test.sh`
- `creek-tools/pyproject.toml` markers
