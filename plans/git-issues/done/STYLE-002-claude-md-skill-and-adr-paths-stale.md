# STYLE-002: `creek-tools/CLAUDE.md` references nonexistent skill / ADR / pylint paths

**Severity:** Low
**Category:** STYLE
**Estimated complexity:** S (≤2h)
**Parallelizable with peers in same category:** yes
**Discovered by:** Dimension 5

## Files affected
- `creek-tools/CLAUDE.md` §5.2 — claims `docs/skills/` (with 9 named files) and `docs/architecture/ADR/`
- `creek-tools/docs/` — neither directory exists

## Dependencies
None.

## Reproduction
```bash
ls creek-tools/docs/skills/ 2>&1   # No such file or directory
ls creek-tools/docs/architecture/  # No such file or directory
```

`creek-tools/CLAUDE.md` lists the skill files as if they exist:
```
docs/skills/architectural-decisions.md
docs/skills/comprehensive-pr-review.md
...
```

The actual skill files live at `.claude/skills/<name>/SKILL.md` (top-level repo, not creek-tools-relative) — typical Claude Code layout.

## Analysis

Three minor doc/code drifts in `CLAUDE.md`:
1. **Skill paths.** §5.2 references `docs/skills/`. Skills are at `.claude/skills/`. Update or remove.
2. **ADR paths.** §5.2 references `docs/architecture/ADR/`. No ADRs exist. Either start writing them or stop claiming a process the project doesn't follow.
3. **Pylint score.** §6.1 claims "Pylint Score: ≥9.0". CI uses `--fail-under=8.0` and `continue-on-error: true` (see CI-002). Pick a number, enforce it, document it.
4. **Maintainability index.** §6.1 claims "Maintainability Index: Minimum 20 (radon)" — `scripts/complexity.sh` runs radon-mi but doesn't enforce the threshold.
5. **Max args / branches / lines per function.** §6.1 claims specific numbers. None enforced.

These are dimension-4 style issues — the docs are over-promising what the toolchain actually checks.

## Proposed remediation

Update `CLAUDE.md` to match reality:
- Remove or fix the `docs/skills/` references; point at `.claude/skills/` if those are the canonical skills.
- Either create at least one ADR demonstrating the process or remove the claim.
- Lower or raise the Pylint threshold to match what CI enforces, then enforce it (CI-002).
- Decide whether to enforce maintainability index / max-args / etc., and either wire them up (radon supports this) or remove the claim.

## Acceptance criteria

- Every path mentioned in `CLAUDE.md` resolves to an actual file or directory.
- Every numeric quality threshold mentioned in `CLAUDE.md` corresponds to a CI gate.
- A regression that violates one of the documented thresholds fails CI.

## References
- `creek-tools/CLAUDE.md` §5.2, §6.1
- CI-002
