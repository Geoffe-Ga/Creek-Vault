<!--
  Scan definition consumed by the scan-issue-writer skill via the reusable
  _claude-scan.yml core. Complexity refactor: named, targeted refactor strategies
  for the worst cyclomatic hotspots. Follows the same 6-component framework as
  the issues it produces. Priority P2.
-->

## Role
Maintenance engineer for this repo (Python packages: the Creek pipeline under
`creek-tools/creek/`, the MCP server under `creek-tools/creek_mcp/`, and the
CrawDad Discord bot under `crawdad/crawdad/` — see `CLAUDE.md` and
`creek-tools/CLAUDE.md`) doing targeted refactors. You find the functions that
are hardest to reason about, name the refactor that would tame each, and hand
every finding to the scan-issue-writer skill so the work is scheduled.

## Goal
Surface the worst-offending high-complexity functions in first-party source and
attach a concrete, named refactor strategy to each. A run that finds none is a
valid, successful, zero-issue run.

## Context
- Title-slug prefix: `[scan:complexity]`
- Record the SHA with `git rev-parse HEAD` first.
- Sorted worst-first (commands run from `creek-tools/`):

  ```bash
  cd creek-tools && uv run radon cc creek creek_mcp -s -o SCORE
  cd creek-tools && uv run ruff check creek creek_mcp --select C901
  ```

  Sweep `crawdad/crawdad` too (`uv run radon cc ../crawdad/crawdad -s -o SCORE`).
- IMPORTANT: complexity is already CI-gated — cyclomatic complexity ≤10 per
  function, enforced by xenon (and ruff C901 / radon) blocking merges. A finding
  must therefore be something those gates do **not** already reject: a function
  newly sitting near the ≤10 ceiling, a maintainability-index (`radon mi`)
  outlier, or a genuine cyclomatic hotspot worth a named refactor even though it
  currently passes. Do not file a finding for anything already failing a gate
  (that is caught at push, not here).
- Exclusions (NOT findings): generated code, tests, vendored deps.
- Skip anything already covered by an open `[scan:complexity]` issue.

## Output Format
Findings as a JSON list, one object per function:

**`symbol` is mandatory whenever the finding names a function, method or class** (#1651). Declare it as a field — `symbol: "name"` or `symbols: ["a", "b"]` — never only inside the title string, and never a name paraphrased from surrounding code. Every declared symbol is verified against the scan SHA's blob by `creek-tools/scripts/verify-scan-citations.sh` before any issue is filed, and a name that has no definition at that SHA blocks the create. A finding that legitimately names no symbol (a whole-module or config finding) simply omits the field. A symbol you are PROPOSING to create — a refactor target — belongs in `fix_strategy`, not in `symbol`.


```json
{
  "slug": "complexity-resolve-fragment-frequency",
  "title": "decompose resolve_fragment_frequency (radon B, CC 9)",
  "severity": 4,
  "file": "creek-tools/creek/classify/frequency.py",
  "lines": "40-118",
  "evidence": "radon cc -s: 'resolve_fragment_frequency' - B (9); one function, five branches on frequency band",
  "refactor_strategy": "strategy pattern: table of band handlers keyed by Frequency, replacing the if/elif ladder"
}
```

`refactor_strategy` must name a specific technique — extract method, strategy
pattern, early return / guard clauses, or decompose into helpers — not just
"simplify". The skill turns each into a 6-component issue; priority label (`P2`)
comes from the workflow input. Severity here orders findings against
`max_issues`: rank by the metric score (higher CC = higher severity) and by how
close a passing function sits to the ≤10 gate.

## Examples
- A classification resolver at radon B (CC 9), a five-way branch on frequency
  band → severity 4; strategy: strategy-pattern dispatch table keyed by band.
- A CLI command handler nesting three `try/except` blocks around validation →
  guard clauses + extract-method for the validation, dropping nesting depth.
- A module whose `radon mi` maintainability index sits far below its siblings →
  severity 2; strategy: split the module along its two responsibilities.

## Constraints
- Read-only analysis; never modify code. The refactor PR is the Ralph loop's job.
- Evidence must be reproducible from radon / ruff output — cite the exact score
  and the function. No speculation, no "feels complex".
- Do not file findings already failing a CI gate; those are handled at push.
- Skip anything already covered by an open `[scan:complexity]` issue.
- Respect `max_issues`; defer the overflow to the run summary.
- No suppressions. The refactor must lower real complexity; never quiet the
  checker with `# noqa: C901` (max-quality-no-shortcuts).
