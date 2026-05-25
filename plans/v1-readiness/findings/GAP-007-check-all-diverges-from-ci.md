# GAP-007 — `./scripts/check-all.sh` does not match CI; CLAUDE.md §1.7 and §2.1 promise that it does

- **Severity:** High
- **Prod-readiness criterion threatened:** doc honesty, unattended reliability

## Evidence

### Interrogate (docstring coverage ≥95%) is gated by CI but not by `check-all.sh`

`creek-tools/scripts/check-all.sh:99-111` runs the following checks in
order:

```
1. Linting (Ruff)
2. Formatting (Ruff)
3. Type checking (MyPy)
4. Pylint
5. Security checks (Bandit + pip-audit)
6. Complexity analysis (Radon)
7. Refurb (modernisation; STYLE-001)
8. Tryceratops (exception hygiene; STYLE-001)
9. Unit tests
10. Coverage report
11. Per-file coverage gate
12. State report size budget
```

No `interrogate` call.

`.github/workflows/ci.yml:109-112`:

```yaml
- name: Check docstring coverage
  run: |
    interrogate -vv \
      --fail-under=${{ env.DOCSTRING_COVERAGE_THRESHOLD }} creek/
```

A developer who drops below 95% docstring coverage can pass
`check-all.sh` locally, push, and fail CI.

### Bandit severity differs between local and CI

`creek-tools/scripts/security.sh:67` (per the gates audit) runs
`bandit -r creek/` — all severities, no `-l` filter.

`.github/workflows/ci.yml:119-121`:

```yaml
- name: Run Bandit security scan
  run: |
    bandit -r creek/ -f json -o reports/bandit-report.json || true
    bandit -r creek/ -ll
```

`-ll` is medium-and-above only. So:

- A low-severity Bandit finding fails locally and passes CI.
- A medium-or-above finding fails both, but with different output
  framing.

The asymmetry is in both directions, which is the worst kind of
divergence to debug.

### `state-budget.sh` skips silently locally and is absent from CI

`creek-tools/scripts/state-budget.sh:56-58`:

```bash
if [[ -z "$VAULT_PATH" ]]; then
    echo "state-budget: no vault path provided ...; skipping."
    exit 0
fi
```

`check-all.sh:105` invokes it. With no `CREEK_VAULT` env var (the
default for most developers), it exits 0 without checking anything. CI
does not invoke it at all.

### The contract

`creek-tools/CLAUDE.md` §1.7 — *"Run `./scripts/check-all.sh` before
every commit. Only commit if exit code is 0."* — promises this script is
the gate.

`creek-tools/CLAUDE.md` §2.1 — *"a fresh checkout runs
`./scripts/check-all.sh` to the same result CI does on the same
commit"* — promises one-to-one parity with CI.

Issue #206 already motivated `dev-setup.sh` to close the *install-time*
side of this contract. The gate-surface drift is the next link in the
same chain.

## Why it matters

Unattended reliability and doc honesty both depend on the developer's
workflow being predictable. The current state has three trapdoors:

1. A clean local pass can fail CI on docstring coverage.
2. A clean local pass can pass CI with a low-severity Bandit finding
   that local rejects — the developer doesn't realize the CI gate is
   weaker than they think.
3. `state-budget.sh` looks like it ran but didn't, so the size-budget
   gate is effectively off for most contributors.

Combined with the prose promises in CLAUDE.md, this is the kind of
gate drift that makes a reviewer doubt every "all green" claim.

## Reproduction

```bash
cd creek-tools
# 1. Verify check-all.sh has no interrogate call:
grep -i interrogate scripts/check-all.sh   # no output

# 2. Verify CI does:
grep -A1 interrogate ../.github/workflows/ci.yml   # ↑ shown above

# 3. Verify Bandit severity divergence:
grep -n "bandit -r creek/" scripts/security.sh ../.github/workflows/ci.yml

# 4. Verify state-budget.sh skips when CREEK_VAULT unset:
unset CREEK_VAULT
bash scripts/state-budget.sh   # prints "skipping." and exits 0
```

## Acceptance criteria

Closed when **all** hold:

1. `check-all.sh` invokes `interrogate -vv --fail-under=95 creek/`
   between steps 8 (tryceratops) and 9 (tests), or factor it into a
   `lint-interrogate.sh` like the other linters.
2. Bandit invocation in `security.sh` and `ci.yml` uses the same
   severity threshold. Recommend `-ll` in both (matching CI's existing
   `bandit -r creek/ -ll`), with a documented carve-out (commented in
   the script) if low-severity findings are intentionally not gated.
3. `state-budget.sh` either:
   - is invoked in CI against a fixture vault (so the local-skip
     behavior is the documented opt-in), **or**
   - is removed from `check-all.sh` and `--help` advertises it as a
     separate opt-in command.
4. `creek-tools/CLAUDE.md` §1.7 and §2.1 prose remain accurate after
   the changes (they don't need rewording if (1)–(3) make them true).
5. A new entry in `CHANGELOG.md` notes the gate-parity fix so other
   contributors don't trip over the old behavior.

## Files affected

- `creek-tools/scripts/check-all.sh`
- `creek-tools/scripts/security.sh`
- `creek-tools/scripts/state-budget.sh` (or invocation site)
- `.github/workflows/ci.yml` (only if Bandit severity reconciled to a
  shared value rather than aligning the local script to CI)
- `creek-tools/CHANGELOG.md`

## Dependencies / blockers

None. Pure tooling change. Risk: aligning Bandit to `-ll` *lowers* the
local gate. If the project would rather raise CI to match the stricter
local script, that's the reverse change — same outcome (parity).
