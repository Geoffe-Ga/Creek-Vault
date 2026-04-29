# DEP-003: pip-audit reports 14 CVEs across transitive dependencies

**Severity:** Medium
**Category:** DEP
**Estimated complexity:** S (≤2h)
**Parallelizable with peers in same category:** yes
**Discovered by:** Running `./scripts/security.sh` locally

## Files affected
- `creek-tools/requirements.txt`
- `creek-tools/requirements-dev.txt`
- `.github/workflows/ci.yml:99-111`

## Dependencies
None.

## Blockers
None for launch (the affected deps are mostly tooling), but for any user installing the project today, pip-audit fails out of the box. CI works around this with `--ignore-vuln` for two CVEs (line 106-110); locally the script has no such ignore list and fails.

## Reproduction
```bash
$ ./scripts/security.sh
...
Name         Version ID                  Fix Versions
cryptography 41.0.7  PYSEC-2024-225      42.0.4
cryptography 41.0.7  CVE-2023-50782      42.0.0
cryptography 41.0.7  CVE-2024-0727       42.0.2
cryptography 41.0.7  GHSA-h4gh-qq45-vh27 43.0.1
cryptography 41.0.7  CVE-2026-26007      46.0.5
cryptography 41.0.7  CVE-2026-34073      46.0.6
pip          24.0    CVE-2025-8869       25.3
pip          24.0    CVE-2026-1703       26.0
pip          24.0    CVE-2026-3219       (no fix)
py           1.11.0  PYSEC-2022-42969    (no fix)
pyjwt        2.7.0   CVE-2026-32597      2.12.0
setuptools   68.1.2  PYSEC-2025-49       78.1.1
setuptools   68.1.2  CVE-2024-6345       70.0.0
wheel        0.42.0  CVE-2026-24049      0.46.2
✗ pip-audit found vulnerable dependencies
```

## Analysis

These are all transitive dependencies of dev tooling (likely pulled in by `pylint`, `bandit`, `interrogate`, etc.) rather than direct runtime deps. Most are exploitable only in unusual configurations (parsing untrusted CSRs, etc.). The CI workflow knows about two of them (PYSEC-2022-42969 and CVE-2026-3219) and ignores them with documented reasoning; the others are unaccounted for.

The right move depends on which ones are real risks for *this* project:
- `cryptography` 41.0.7: pulled in by `requests` ↔ `urllib3` ↔ `pip`. Several of the CVEs are remote (network-side TLS). For a local-first tool, low risk; but worth tracking.
- `pyjwt` 2.7.0: not used directly. CVE-2026-32597 affects JWT validation — irrelevant unless we add JWT auth.
- `setuptools`, `wheel`, `pip`, `py`: build/dev tooling.

For the ones with fix versions, just bump. For the ones without, add a documented `--ignore-vuln` with a justification (just like the CI already does for two of them).

Confidence: verified by running locally.

## Proposed remediation

1. Bump `cryptography>=46.0.6` (whichever transitive pulls it; possibly via `bandit` or `pip-audit` themselves).
2. Bump `pyjwt>=2.12.0`, `setuptools>=78.1.1`, `wheel>=0.46.2`.
3. For `pip` (no available fix), add `--ignore-vuln CVE-2026-3219` with a comment matching the CI workflow's reasoning. Same for `py PYSEC-2022-42969`.
4. Add the same `--ignore-vuln` set to `creek-tools/scripts/security.sh` so local and CI agree.
5. Set up Dependabot (or similar) to surface future CVEs proactively.

## Acceptance criteria

- `./scripts/security.sh` exits 0.
- CI's pip-audit step exits 0 without `|| true`.
- Local and CI use the same ignore list.
- A test or CI step verifies that the ignore list is non-empty only for documented unfixable CVEs.

## References
- `.github/workflows/ci.yml:99-111`
- `creek-tools/scripts/security.sh`
- pip-audit output above
