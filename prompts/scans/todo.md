<!--
  Scan definition consumed by the scan-issue-writer skill via the reusable
  _claude-scan.yml core. This is the tracer scan of the autonomous maintenance
  pipeline: the cheapest and most deterministic, wired end-to-end first.
  Follows the same 6-component framework as the issues it produces.
-->

## Role
Maintenance engineer for this repo (Python packages: the Creek pipeline under
`creek-tools/creek/`, the MCP server under `creek-tools/creek_mcp/`, and the
CrawDad Discord bot under `crawdad/crawdad/` — see `CLAUDE.md` and
`creek-tools/CLAUDE.md`). You convert inline deferred-work markers into
tracked, agent-ready GitHub issues so the work is scheduled instead of rotting
in a comment.

## Goal
Find every `TODO` / `FIXME` / `HACK` / `XXX` marker in first-party source and
hand each — with its file:line and enough surrounding context to act on — to
the scan-issue-writer skill as a finding. A run that finds none is a valid,
successful, zero-issue run.

## Context
- Title-slug prefix: `[scan:todo]`
- Search first-party source only:
  - `creek-tools/creek/`
  - `creek-tools/creek_mcp/`
  - `crawdad/crawdad/`
- Command (record the SHA with `git rev-parse HEAD` first):

  ```bash
  grep -rInE '\b(TODO|FIXME|HACK|XXX)\b' creek-tools/creek creek-tools/creek_mcp crawdad/crawdad
  ```

- Exclusions (NOT findings):
  - Generated code, lockfiles, vendored deps, build output.
  - Markers inside test fixtures that deliberately assert on the literal string.
  - Markers already tracked by an open `[scan:todo]` issue (the skill dedupes).
- One marker that clearly belongs to a cluster of related markers in the same
  module may be filed as a single issue covering the cluster, with every
  file:line listed in Context.

## Output Format
Findings as a JSON list, one object per marker (or marker cluster):

**`symbol` is mandatory whenever the finding names a function, method or class** (#1651). Declare it as a field — `symbol: "name"` or `symbols: ["a", "b"]` — never only inside the title string, and never a name paraphrased from surrounding code. Every declared symbol is verified against the scan SHA's blob by `creek-tools/scripts/verify-scan-citations.sh` before any issue is filed, and a name that has no definition at that SHA blocks the create. A finding that legitimately names no symbol (a whole-module or config finding) simply omits the field. A symbol you are PROPOSING to create — a refactor target — belongs in `fix_strategy`, not in `symbol`.

```json
{
  "slug": "todo-resonance-batch-embeddings",
  "title": "TODO: batch embedding calls in link.resonance scoring",
  "severity": 3,
  "file": "creek-tools/creek/link/resonance.py",
  "lines": "142",
  "symbol": "score_resonance",
  "marker": "TODO",
  "evidence": "the grep hit plus 3–5 lines of surrounding code",
  "goal": "one measurable sentence for the issue's Goal component"
}
```

The skill turns each into a 6-component issue. Priority label comes from the
workflow input (`P3` for this scan per the task table); severity here only
orders the findings against `max_issues`.

## Examples
- `FIXME: this ignores the timezone` above a date-parse call → severity 4
  (potential correctness bug); Goal names the timezone-correct fix + a test.
- `TODO: extract this into a helper` in a large CLI command module → severity 2
  (maintainability); Goal names the helper and the commands that consume it.
- `XXX: remove after the source_key migration lands` where that migration has
  already merged → severity 3; Goal is to delete the dead branch and the marker.

## Constraints
- Read-only analysis; never modify code. (The follow-up PR that removes the
  marker in favor of the issue link is the Ralph loop's job, not this scan's.)
- Evidence must be a real grep hit with surrounding context — no speculative
  markers, no markers you cannot cite by file:line.
- Skip anything already covered by an open `[scan:todo]` issue.
- Respect `max_issues`; defer the overflow to the run summary.
