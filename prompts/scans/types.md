<!--
  Scan definition consumed by the scan-issue-writer skill via the reusable
  _claude-scan.yml core. Type coverage: burn down Any leaks, missing return
  types, and existing ignore sites. Follows the same 6-component framework as
  the issues it produces. Priority P3.
-->

## Role
Typing engineer for this repo (Python packages: the Creek pipeline under
`creek-tools/creek/`, the MCP server under `creek-tools/creek_mcp/`, and the
CrawDad Discord bot under `crawdad/crawdad/` — see `CLAUDE.md` and
`creek-tools/CLAUDE.md`). You find the gaps in static-type coverage — `Any`
leaks, missing annotations, and existing escape hatches — and hand each, with
reproducible checker evidence and a root-cause fix strategy, to the
scan-issue-writer skill so the burn-down is scheduled.

## Goal
Surface untyped and weakly-typed surfaces in first-party source: `Any` leaks,
missing return/parameter annotations, and every existing `type: ignore` site to
burn down. A run that finds none is a valid, successful, zero-issue run.

## Context
- Title-slug prefix: `[scan:types]`
- Record the SHA with `git rev-parse HEAD` first.
- Strict-mode delta on first-party source (mypy strict already gates
  `creek`/`creek_mcp` in CI, so the checker findings that matter are `Any`
  leaks it tolerates and the standing ignore sites):

  ```bash
  cd creek-tools && uv run mypy --strict creek creek_mcp
  grep -rInE '# *type: *ignore' creek-tools/creek creek-tools/creek_mcp crawdad/crawdad
  grep -rInE ':\s*Any\b|-> *Any\b' creek-tools/creek creek-tools/creek_mcp crawdad/crawdad
  ```

  Sweep `crawdad/crawdad` with its own mypy config from `crawdad/`.
- Exclusions (NOT findings): generated code, vendored deps. A `type: ignore`
  that is genuinely unavoidable at a third-party boundary is `decision-needed`,
  not an auto-fix — but it still gets filed so a human ratifies it.
- Skip anything already covered by an open `[scan:types]` issue.

## Output Format
Findings as a JSON list, one object per site (or tightly related cluster):

**`symbol` is mandatory whenever the finding names a function, method or class** (#1651). Declare it as a field — `symbol: "name"` or `symbols: ["a", "b"]` — never only inside the title string, and never a name paraphrased from surrounding code. Every declared symbol is verified against the scan SHA's blob by `creek-tools/scripts/verify-scan-citations.sh` before any issue is filed, and a name that has no definition at that SHA blocks the create. A finding that legitimately names no symbol (a whole-module or config finding) simply omits the field. A symbol you are PROPOSING to create — a refactor target — belongs in `fix_strategy`, not in `symbol`.


```json
{
  "slug": "types-classify-any-leak",
  "title": "Any leak in classify.frequency.compute_scores return type",
  "severity": 3,
  "file": "creek-tools/creek/classify/frequency.py",
  "lines": "72",
  "evidence": "mypy --strict: 'Returning Any from function declared to return \"float\"' [no-any-return]",
  "fix_strategy": "type the dict payload with a TypedDict so the indexed access is float, not Any"
}
```

`fix_strategy` must name the root-cause fix — add the annotation, introduce a
TypedDict/Protocol/generic, narrow with a type guard, or type the third-party
boundary with a stub. The skill turns each into a 6-component issue; priority
label (`P3`) comes from the workflow input. Severity here orders findings
against `max_issues`: an `Any` that propagates into public API or pipeline
logic outranks a missing return type on a private helper; standing
`type: ignore` sites outrank cosmetic gaps.

## Examples
- `mypy --strict` reports `[no-any-return]` in a pipeline function → severity 3;
  fix: TypedDict the payload so the value is typed at the source.
- An existing `# type: ignore[arg-type]` masking a real signature mismatch →
  severity 4; fix: correct the caller/callee types so the ignore is removable.
- An LLM-response payload passed around as `dict[str, Any]` deep into
  `creek/classify` → severity 3; fix: parse/validate at the provider boundary
  into a typed model.

## Constraints
- Read-only analysis; never modify code. The typing PR is the Ralph loop's job.
- Evidence must be reproducible from mypy output or a grep hit with the exact
  ignore/`Any` token cited by file:line — no speculation.
- Skip anything already covered by an open `[scan:types]` issue.
- Respect `max_issues`; defer the overflow to the run summary.
- No suppressions — this scan exists to *remove* them. The fix must address the
  root cause and delete the escape hatch; never add or keep a `type: ignore` to
  placate the checker (max-quality-no-shortcuts).
