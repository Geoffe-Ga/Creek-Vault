<!--
  Scan definition consumed by the scan-issue-writer skill via the reusable
  _claude-scan.yml core. Documentation-drift sweep of this repo: find docs that
  no longer match the code at HEAD and hand each to the skill as a finding.
  Follows the same 6-component framework as the issues it produces.
-->

## Role
Technical writer / engineer for this repo (Python packages: the Creek pipeline
under `creek-tools/creek/`, the MCP server under `creek-tools/creek_mcp/`, and
the CrawDad Discord bot under `crawdad/crawdad/` — see `CLAUDE.md` and
`creek-tools/CLAUDE.md`). You find where the prose lies — where a docstring,
README, north-star doc, or module doc claims behavior that no longer matches
the code at HEAD — and hand each drift to the scan-issue-writer skill.

## Goal
Surface documentation that has drifted from the code so each becomes a tracked,
agent-ready fix. Focus on ACCURACY, not presence: `interrogate` already gates
docstring coverage at ≥95%, so a missing docstring is rarely the finding — a
docstring that describes the wrong parameters, return type, or behavior is. A
run that finds none is a valid, successful, zero-issue run.

## Context
- Title-slug prefix: `[scan:docs]`
- Priority label for this scan (workflow input): `P3`
- Record the SHA with `git rev-parse HEAD` before scanning; every issue cites it.
- What counts as a finding:
  - **Docstring drift** — a Python docstring in `creek-tools/creek/`,
    `creek-tools/creek_mcp/`, or `crawdad/crawdad/` whose documented args,
    return type, raised exceptions, or described behavior contradicts the
    actual function signature or body at HEAD.
  - **Stale prose claims** — a concrete claim in `README.md`, `CLAUDE.md`,
    `creek-tools/CLAUDE.md`, or a doc under `docs/` (a CLI command, file path,
    script name, config key, threshold number) that no longer matches the tree.
    Grep the claim, then verify it against the actual file / command / script.
  - **Documented-CLI drift** — a `creek <subcommand>` invocation documented in
    the README, `CLAUDE.md`, or `docs/` that does not match the Typer commands
    actually registered in `creek/cli` (renamed subcommand, removed option,
    changed default).
  - **Undocumented public API** — a public Typer command, MCP tool, or public
    module with no docstring where its siblings have one.
  - **Stale code examples** — a fenced code block in docs that would fail if run
    (wrong import path, renamed symbol, changed argument order).
- Useful commands (read-only):

  ```bash
  grep -rInE '(creek-tools/creek|creek-tools/scripts|crawdad/crawdad|scripts/)[A-Za-z0-9_./-]+' \
    README.md CLAUDE.md creek-tools/CLAUDE.md docs
  grep -rInE '\bcreek [a-z][a-z-]+' README.md CLAUDE.md creek-tools/CLAUDE.md docs
  # then diff documented subcommands against the Typer registrations in creek/cli
  ```

- Exclusions (NOT findings): generated code, lockfiles, vendored deps, build
  output; pure formatting nits; and anything already covered by an open
  `[scan:docs]` issue (the skill dedupes).

## Output Format
Findings as a JSON list, one object per drift:

**`symbol` is mandatory whenever the finding names a function, method or class** (#1651). Declare it as a field — `symbol: "name"` or `symbols: ["a", "b"]` — never only inside the title string, and never a name paraphrased from surrounding code. Every declared symbol is verified against the scan SHA's blob by `creek-tools/scripts/verify-scan-citations.sh` before any issue is filed, and a name that has no definition at that SHA blocks the create. A finding that legitimately names no symbol (a whole-module or config finding) simply omits the field. A symbol you are PROPOSING to create — a refactor target — belongs in `fix_strategy`, not in `symbol`.

```json
{
  "slug": "docs-resonance-docstring-drift",
  "title": "score_resonance docstring documents removed `threshold` arg",
  "severity": 3,
  "file": "creek-tools/creek/link/resonance.py",
  "lines": "48-72",
  "symbol": "score_resonance",
  "evidence": "docstring lists args (fragment, threshold); signature at HEAD is score_resonance(fragment, top_k) — threshold removed in <SHA>",
  "before_after_sketch": "docstring Args section → match the real (fragment, top_k) signature and describe top_k"
}
```

Severity is 1–5. It orders findings against `max_issues`; the priority label
comes from the workflow input.

## Examples
- A README quick-start that runs a plain `pip install` when the canonical
  install is `cd creek-tools && uv sync --all-extras` → severity 3; sketch
  corrects the command.
- `CLAUDE.md` documenting a `creek process` subcommand that `creek/cli` no
  longer registers → severity 2; sketch names the real per-type ingest flow.
- A pipeline docstring promising `returns None on miss` when the code raises
  `FragmentNotFoundError` → severity 3; sketch rewrites the Returns/Raises
  section.

## Constraints
- Read-only analysis; never modify code or docs.
- Evidence must be reproducible: cite the doc line AND the contradicting code
  line (file:line at the scanned SHA). No speculative "this looks outdated" — if
  you cannot show both sides of the mismatch, it is not a finding.
- Skip anything already covered by an open `[scan:docs]` issue.
- Respect `max_issues`; defer the overflow to the run summary.
