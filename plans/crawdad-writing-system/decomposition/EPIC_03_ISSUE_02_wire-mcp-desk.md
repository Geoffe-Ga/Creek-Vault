## Role

You are a senior Python engineer working in `creek-tools/creek_mcp/` and `creek/author/`.

## Goal

Wire the `author` MCP tool to the real Creek Writing Desk so an MCP client receives a genuine cited,
voiced, reflection-gated draft (or an ESCALATE signal) instead of the stub.

## Context

- **Parent epic:** #EPIC_03_NUMBER
- **Predecessor issue(s):** #EPIC_03_ISSUE_01_NUMBER (stub verb), and EPIC_02 desk merged.
- **SPEC section:** §10 (MCP), §4.3 (single source of truth — both surfaces call the same desk).
- **Files involved:**
  - `creek-tools/creek_mcp/` — replace the stub with a call into `run_author(...)`.
  - `creek-tools/tests/` — integration test through the MCP boundary.
- **Prior decisions:** The MCP verb is a thin adapter — no business logic; it delegates to `creek/author/run_author`. Privacy `include_tier` is honored end-to-end.
- **State of the world:** The verb returns a stub; the real desk exists from EPIC_02.

## Output Format

A single PR containing:

- [ ] `author` verb delegates to the real desk and returns its result over stdio.
- [ ] Integration test: a research question on a fixture vault returns a cited draft via MCP.
- [ ] `--include-tier` / privacy gate honored across the boundary.

## Examples

```python
resp = await mcp_client.call_tool("author", {"medium": "research", "query": "F6 medicine vs toxic", "vault": fixture})
assert resp["verdict"] == "PASS"
assert all(claim["source_fragments"] for claim in resp["claims"])
```

## Constraints

**Scope fence:** Do not touch CrawDad routing (ISSUE_03) or allowlist/degradation (ISSUE_04). Adapter
only — no desk logic in the MCP layer.

**Anti-bypass (verbatim, non-negotiable):**

> No `noqa`, `# type: ignore`, `pylint: disable`, `eslint-disable`, or equivalent
> linter/type-checker silencers. Fix the root cause. The only exception is the documented 4-line
> escape hatch (third-party library bug / language-version compatibility / benchmarked
> performance necessity / generated code) — and it must include the reason, a reference URL, an
> alternative considered, and a review date. See the `max-quality-no-shortcuts` skill.

**Tracer-code invariant:** Claude Code can now author via MCP end-to-end; existing verbs unaffected.

## Definition of Done (stay-green)

- [ ] All new and existing tests pass (`cd creek-tools && ./scripts/test.sh --all`).
- [ ] `pre-commit run --all-files -c creek-tools/.pre-commit-config.yaml` is clean.
- [ ] Coverage on changed lines ≥90% branch; docstrings ≥95%; complexity ≤10; MyPy strict.
- [ ] PR body includes `Refs #EPIC_03_NUMBER` and `Closes #THIS_ISSUE_NUMBER`.
- [ ] Latest Claude reviewer `Verdict:` on HEAD is `LGTM`.

## Labels

`spec-decomposition`, `core`, `mcp`
