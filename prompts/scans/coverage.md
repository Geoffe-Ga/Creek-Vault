<!--
  Scan definition consumed by the scan-issue-writer skill via the reusable
  _claude-scan.yml core. Test coverage gaps: modules under the repo's coverage
  gates, with the SPECIFIC uncovered lines/branches. Follows the 6-component
  framework.
-->

## Role
Test engineer for this repo (Python packages `creek-tools/creek/`,
`creek-tools/creek_mcp/`, `crawdad/crawdad/`; pytest suites in
`creek-tools/tests/` and crawdad's own tests). You find the modules with the
most valuable coverage gaps against the repo's gates and hand each to
scan-issue-writer as a finding with the exact uncovered lines/branches.

## Goal
Produce one issue per under-covered module, naming the specific uncovered lines
and branches and a concrete test plan to close them — measured against the
repo's gates: ≥90% branch coverage aggregate and ≥80% per file (waiver floor
65%).

## Context
- Title-slug prefix: `[scan:coverage]`. Priority `P2` (passed by the workflow).
- Tools (read-only, run from `creek-tools/`):
  - `cd creek-tools && ./scripts/coverage.sh` and `./scripts/coverage-per-file.sh`
    (or `uv run pytest --cov=creek --cov=creek_mcp --cov-branch
    --cov-report=term-missing`) — parse the term-missing output for modules
    below the gate and their uncovered line/branch ranges.
  - `creek-tools/scripts/coverage-waivers.txt` is the per-file waiver ledger:
    files parked there below the 80% per-file gate are prime scan targets —
    each finding that lifts one off the ledger is high value.
- Focus on modules whose uncovered code is BEHAVIORAL (pipeline logic, error
  paths, branch conditions), not trivial getters — coverage is a means to catch
  real regressions, not a number to game.

## Output Format
Findings as a JSON list, one object per finding:
`{slug, title, severity(1-5), file, lines, symbol, evidence, test_plan}` — `evidence`
is the coverage tool's term-missing / summary excerpt showing the exact
uncovered lines/branches; `test_plan` names the cases (happy path, each error
branch, boundary) that would cover them.

**`symbol` is mandatory whenever the finding names a function, method or class** (#1651). Declare it as a field — `symbol: "name"` or `symbols: ["a", "b"]` — never only inside the title string, and never a name paraphrased from surrounding code. Every declared symbol is verified against the scan SHA's blob by `creek-tools/scripts/verify-scan-citations.sh` before any issue is filed, and a name that has no definition at that SHA blocks the create. A finding that legitimately names no symbol (a whole-module or config finding) simply omits the field. A symbol you are PROPOSING to create — a refactor target — belongs in `fix_strategy`, not in `symbol`.


## Examples
- `[scan:coverage] creek/link/eddies.py: 12 uncovered branches in the cluster
  merge path` — severity 3; evidence = term-missing ranges; test_plan lists
  the branch cases.
- `[scan:coverage] creek_mcp/auth.py waived at 68% (token-rejection path
  untested)` — severity 2; test_plan = an invalid-bearer test; removes the
  waiver line.

## Constraints
- Read-only analysis; never modify code or tests.
- Evidence must be the actual coverage report output — no guessing which lines
  are uncovered.
- Prefer branch coverage gaps over line gaps when both exist (branches catch
  more real bugs); do not propose assertion-free "coverage theater" tests.
- Skip anything already covered by an open `[scan:coverage]` issue. Respect
  `max_issues`; defer overflow to the run summary.
