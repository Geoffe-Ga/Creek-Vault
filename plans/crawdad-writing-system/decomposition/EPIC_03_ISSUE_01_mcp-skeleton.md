## Role

You are a senior Python engineer working in `creek-tools/creek_mcp/`, fluent in this repo's MCP
surface (stdio) that Claude Code and CrawDad consume.

## Goal

Add an `author` MCP tool that mirrors the `creek author` CLI args and returns a typed stub response
(draft + provenance + reflection verdict) over stdio, so an MCP client can call it and receive the
correct shape before it is wired to the real desk.

## Context

- **Parent epic:** #EPIC_03_NUMBER
- **Predecessor issue(s):** none — skeleton issue for EPIC_03. (Depends on EPIC_02 existing for the response types.)
- **SPEC section:** §10 (CLI & MCP surface), §4.3 (adapters).
- **Files involved:**
  - `creek-tools/creek_mcp/` — register the `author` tool.
  - reuse `AuthoredDraft` type from `creek/author/models.py`.
  - `creek-tools/docs/mcp.md` — document the new verb.
  - `creek-tools/tests/` — MCP tool smoke test.
- **Prior decisions:** The MCP verb must accept the same args as the CLI (`medium`, `query`, `max_rounds`, `include_tier`, `dry_run`) and return `draft + provenance + verdict`. Stub now; real wiring is ISSUE_02.
- **State of the world:** The MCP surface exposes existing verbs (state, lint, mine, save, …) but no `author`.

## Output Format

A single PR containing:

- [ ] `author` MCP tool returning a typed stub response over stdio.
- [ ] `docs/mcp.md` entry for the verb.
- [ ] Smoke test: an in-process MCP client calls `author` and gets the declared shape.

## Examples

```python
resp = await mcp_client.call_tool("author", {"medium": "research", "query": "What is F6?"})
assert resp["verdict"] in {"PASS", "REVISE", "ESCALATE"}
assert "provenance" in resp
```

## Constraints

**Scope fence:** Stub response only — do not call the real desk (ISSUE_02). Do not touch CrawDad
(ISSUE_03).

**Anti-bypass (verbatim, non-negotiable):**

> No `noqa`, `# type: ignore`, `pylint: disable`, `eslint-disable`, or equivalent
> linter/type-checker silencers. Fix the root cause. The only exception is the documented 4-line
> escape hatch (third-party library bug / language-version compatibility / benchmarked
> performance necessity / generated code) — and it must include the reason, a reference URL, an
> alternative considered, and a review date. See the `max-quality-no-shortcuts` skill.

**Tracer-code invariant:** The MCP surface gains one new verb returning a valid shape; all existing
verbs are unchanged.

## Definition of Done (stay-green)

- [ ] All new and existing tests pass (`cd creek-tools && ./scripts/test.sh --all`).
- [ ] `pre-commit run --all-files -c creek-tools/.pre-commit-config.yaml` is clean.
- [ ] Coverage on changed lines ≥90% branch; docstrings ≥95%; complexity ≤10; MyPy strict.
- [ ] PR body includes `Refs #EPIC_03_NUMBER` and `Closes #THIS_ISSUE_NUMBER`.
- [ ] Latest Claude reviewer `Verdict:` on HEAD is `LGTM`.

## Labels

`spec-decomposition`, `tracer-skeleton`, `mcp`
