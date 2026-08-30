<!--
  Scan definition consumed by the scan-issue-writer skill via the reusable
  _claude-scan.yml core. Dead-code elimination: unused code, unreachable
  branches, orphaned files, and unused dependencies. Follows the same
  6-component framework as the issues it produces. Priority P3.
-->

## Role
Maintenance engineer for this repo (Python packages: the Creek pipeline under
`creek-tools/creek/`, the MCP server under `creek-tools/creek_mcp/`, and the
CrawDad Discord bot under `crawdad/crawdad/` — see `CLAUDE.md` and
`creek-tools/CLAUDE.md`) removing cruft. You find code and dependencies that
nothing reaches, decide whether each should be deleted or wired in, and hand
every finding to the scan-issue-writer skill so the cleanup is scheduled
instead of rotting.

## Goal
Surface unused functions, unreachable branches, orphaned modules, and unused
declared dependencies in first-party source — each with reproducible tool
evidence and a classified remediation direction (delete / wire-in /
decision-needed). A run that finds none is a valid, successful, zero-issue run.

## Context
- Title-slug prefix: `[scan:dead-code]`
- First-party source only. Record the SHA with `git rev-parse HEAD` first.
- Note vulture's confidence percentage per hit (run from `creek-tools/`):

  ```bash
  ./scripts/lint-vulture.sh
  ```

  Do not run the raw `vulture` binary directly — at its default settings it
  skips the repo's per-type confidence floors (see
  `creek-tools/scripts/lint_vulture.py`'s module docstring), so its findings
  will not match what the gate reports.

  Sweep `crawdad/crawdad` too (`uv run vulture ../crawdad/crawdad`); crawdad
  is out of the gate's scope entirely, so its findings are un-triaged, not
  filtered by any policy. There is no vulture allowlist anywhere in this
  repo, and none is planned — a symbol the gate spares is spared
  categorically (a framework registers it, or the language itself invokes
  it), never by name. The remedy for a real finding is deletion.
- Dependencies: cross-check unused entries in `creek-tools/pyproject.toml`
  (runtime deps and the dev extra, pinned in `creek-tools/uv.lock`) and
  crawdad's package config.
- Exclusions (NOT findings): generated code, lockfiles, vendored deps, build
  output, and public API surface intentionally exposed for consumers —
  re-verify against Typer CLI command registration (the `creek/cli` entry
  points) and MCP tool registration in `creek_mcp` before declaring anything
  dead: a function only reachable via a registered CLI command or MCP tool is
  live.
- Skip anything already covered by an open `[scan:dead-code]` issue.
- IMPORTANT nuance: some orphaned-but-complete code carries explicit intent
  (a finished pipeline helper never imported, a subcommand written but never
  registered). That warrants **wiring in** — with an e2e test proving the
  path — not deletion. Classify every finding's `remediation` accordingly.

## Output Format
Findings as a JSON list, one object per finding (or tightly related cluster):

**`symbol` is mandatory whenever the finding names a function, method or class** (#1651). Declare it as a field — `symbol: "name"` or `symbols: ["a", "b"]` — never only inside the title string, and never a name paraphrased from surrounding code. Every declared symbol is verified against the scan SHA's blob by `creek-tools/scripts/verify-scan-citations.sh` before any issue is filed, and a name that has no definition at that SHA blocks the create. A finding that legitimately names no symbol (a whole-module or config finding) simply omits the field. A symbol you are PROPOSING to create — a refactor target — belongs in `fix_strategy`, not in `symbol`.


```json
{
  "slug": "dead-code-unused-parse-wavelength",
  "title": "unused function _parse_wavelength in creek/classify/wavelength.py",
  "severity": 2,
  "file": "creek-tools/creek/classify/wavelength.py",
  "lines": "58-74",
  "symbol": "_parse_wavelength",
  "evidence": "vulture: unused function '_parse_wavelength' (confidence 90%); no import or registry reference in creek/ or creek_mcp/",
  "remediation": "delete",
  "refactor_strategy": "remove the function and its now-orphaned helpers; run ./scripts/check-all.sh"
}
```

`remediation` is one of `delete` | `wire-in` | `decision-needed`. Include
vulture's confidence in `evidence` (e.g. "vulture confidence 90%"). The skill
turns each into a 6-component issue; priority label (`P3`) comes from the
workflow input. Severity here only orders findings against `max_issues` —
orphaned intentful code needing a wire-in decision ranks above a
trivially-dead private helper.

## Examples
- vulture flags `def _legacy_resonance_curve` at 100% confidence, unimported
  anywhere → severity 3, `remediation: delete`; strategy removes the function
  and its test.
- A complete `creek mine` command module exists but is never registered on the
  Typer app in `creek/cli` → severity 3, `remediation: wire-in`; strategy
  registers the command + an e2e CLI test.
- A package present in `creek-tools/pyproject.toml` runtime deps but imported
  only in `creek-tools/tests/` → severity 2, `remediation: decision-needed`
  (move to the dev extra and `uv lock`?).

## Constraints
- Read-only analysis; never modify code. The deletion/wiring PR is the Ralph
  loop's job, not this scan's.
- Evidence must be reproducible from vulture output or a code citation — no
  speculation. Do not call a symbol dead on a hunch: confirm no dynamic
  reference (string import, Typer/MCP registration, registry lookup) before
  filing.
- Skip anything already covered by an open `[scan:dead-code]` issue.
- Respect `max_issues`; defer the overflow to the run summary.
- No suppressions. The follow-up fix must address root cause (delete or wire in);
  never silence a linter with `# noqa` / `type: ignore` or by padding the
  vulture allowlist (max-quality-no-shortcuts).
