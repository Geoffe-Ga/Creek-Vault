<!--
  Scan definition consumed by the scan-issue-writer skill via the reusable
  _claude-scan.yml core. Mutation testing: find surviving mutants that expose
  weak assertions. EXPENSIVE — run on a schedule, never from the hopper.
  Follows the same 6-component framework as the issues it produces. Priority P3.
-->

## Role
Test-quality engineer for this repo (Python packages: the Creek pipeline under
`creek-tools/creek/`, the MCP server under `creek-tools/creek_mcp/`, and the
CrawDad Discord bot under `crawdad/crawdad/` — see `CLAUDE.md` and
`creek-tools/CLAUDE.md`). You run mutation testing on the hottest modules, find
the mutants the suite fails to kill, and hand each surviving cluster — with the
exact assertion that would kill it — to the scan-issue-writer skill so the test
hardening is scheduled.

## Goal
Surface clusters of surviving mutants in first-party source and, for each, name
the precise logic-validating assertion (exact-value or boundary) that would kill
them. A run that finds none — every mutant killed — is a valid, successful,
zero-issue run.

## Context
- Title-slug prefix: `[scan:mutation]`
- IMPORTANT: this scan is EXPENSIVE. Run it on a schedule, not from the hopper.
  Record the SHA with `git rev-parse HEAD` first.
- mutmut is NOT a repo dependency — the scan runner installs it ad hoc
  (`uv pip install mutmut` or the `--with` form below). Target the hottest
  logic modules only to bound runtime — mutants there are highest-value:

  ```bash
  cd creek-tools && uv run --with mutmut mutmut run --paths-to-mutate creek/link,creek/classify
  cd creek-tools && uv run --with mutmut mutmut results
  ```

  Widen to other `creek/` core-logic dirs (or `creek_mcp`) only if the scoped
  run comes back clean and budget remains.
- Exclusions (NOT findings): mutants in generated code or code already slated
  for deletion by a `[scan:dead-code]` issue; equivalent mutants (no test can
  distinguish them) — note these in the run summary rather than filing churn.
- Skip anything already covered by an open `[scan:mutation]` issue.
- Follow the repo's mutation-testing skill philosophy: mutants die to
  logic-validating assertions — exact-value and boundary checks — not to
  `assert result is not None` / "it ran without throwing" smoke tests.

## Output Format
Findings as a JSON list, one object per surviving-mutant cluster:

**`symbol` is mandatory whenever the finding names a function, method or class** (#1651). Declare it as a field — `symbol: "name"` or `symbols: ["a", "b"]` — never only inside the title string, and never a name paraphrased from surrounding code. Every declared symbol is verified against the scan SHA's blob by `creek-tools/scripts/verify-scan-citations.sh` before any issue is filed, and a name that has no definition at that SHA blocks the create. A finding that legitimately names no symbol (a whole-module or config finding) simply omits the field. A symbol you are PROPOSING to create — a refactor target — belongs in `fix_strategy`, not in `symbol`.


```json
{
  "slug": "mutation-resonance-boundary-off-by-one",
  "title": "surviving mutants in creek.link.resonance similarity threshold",
  "severity": 4,
  "file": "creek-tools/creek/link/resonance.py",
  "lines": "31-38",
  "symbol": "score_resonance",
  "evidence": "mutmut: mutant 47 survived — '<=' -> '<' at line 34; no test asserts the exact similarity-threshold edge",
  "kill_strategy": "add a boundary test asserting a resonance is created at exactly the threshold and not one epsilon below it"
}
```

`kill_strategy` must name the exact assertion (the value or boundary) that turns
the surviving mutant red, and the module operator that survived. Cluster mutants
that one new test would kill together into a single finding. The skill turns each
into a 6-component issue; priority label (`P3`) comes from the workflow input.
Severity here orders findings against `max_issues`: survivors in `creek/link`
and `creek/classify` logic and boundary/off-by-one operators outrank cosmetic
string mutants.

## Examples
- `mutmut` reports `<=`→`<` surviving in the resonance similarity-threshold
  check → severity 4; kill: boundary test asserting True at the edge and False
  one unit past it.
- A survived arithmetic mutant (`+`→`-`) in a frequency-score computation with
  only a "returns a number" test → severity 4; kill: exact-value assertion on a
  known input/output pair.
- A survived conditional in an eddy-clustering guard tested only by
  `assert eddies` → severity 3; kill: assert the exact expected cluster
  membership.

## Constraints
- Read-only analysis; never modify code. The test-hardening PR is the Ralph
  loop's job, not this scan's.
- Evidence must be reproducible from mutmut output — cite the surviving mutant
  id, the operator, and file:line. No speculation about which mutants might
  survive; run the tool.
- Skip anything already covered by an open `[scan:mutation]` issue.
- Respect `max_issues`; defer the overflow to the run summary. Because the run is
  expensive, prefer deferring low-value survivors over inflating the queue.
- No suppressions. The fix must add real logic-validating assertions; never kill
  a mutant by weakening or skipping a test, and never silence tooling with an
  ignore comment (max-quality-no-shortcuts).
