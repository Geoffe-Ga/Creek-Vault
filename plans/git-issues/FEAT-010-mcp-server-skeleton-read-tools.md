# FEAT-010: MCP server skeleton + read tools

**Severity:** High (v1.0)
**Category:** FEAT
**Estimated LOC:** ~500
**Estimated complexity:** M
**Source candidate:** [`plans/2026-05-05_comparative-analysis/candidates/ADAPT-004-mcp-server-as-interface.md`](../2026-05-05_comparative-analysis/candidates/ADAPT-004-mcp-server-as-interface.md) (part 1 of 3)
**Dependencies:** FEAT-006/007 (`creek state`), FEAT-008 (`creek lint`), FEAT-004 (compiled-layer-aware `mine`/`draft`)
**Parallelizable with peers:** no (FEAT-011 and FEAT-012 extend this)
**Wave:** 4

## Goal

Stand up a `creek-tools-mcp` server that exposes the existing read-only CLI surface as MCP tools. Both CrawDad (FEAT-013+) and the developer's Claude Code consume the same surface. Privacy-tier ceiling enforcement happens at the MCP boundary, not as a downstream check.

## Files to touch

- `creek-tools/creek_mcp/__init__.py` (new package, sibling to `creek/`).
- `creek-tools/creek_mcp/server.py` (new) — MCP server bootstrap; uses the official `mcp` Python SDK.
- `creek-tools/creek_mcp/tools/state.py` (new) — wraps `creek state`.
- `creek-tools/creek_mcp/tools/lint.py` (new) — wraps `creek lint`.
- `creek-tools/creek_mcp/tools/mine.py` (new) — wraps `creek mine`.
- `creek-tools/creek_mcp/tools/draft.py` (new) — wraps `creek draft`.
- `creek-tools/creek_mcp/tools/state_read.py` (new) — `creek.state.read` returns the latest `00-Creek-Meta/State/latest.md` content (cheap, no re-render).
- `creek-tools/creek_mcp/tier_ceiling.py` (new) — privacy-tier enforcement helper used by every read tool.
- `creek-tools/pyproject.toml` — add `mcp` dependency, declare `creek-tools-mcp` entry point: `creek-tools-mcp = "creek_mcp.server:main"`.
- `creek-tools/tests/test_mcp_server.py` (new) — server bootstrap test + per-tool integration tests.
- `creek-tools/docs/mcp.md` (new) — registration instructions for Claude Code, Claude Desktop, and CrawDad.

## Pre-decided choices

- **MCP SDK:** the official Anthropic Python `mcp` library (whatever the current pinned version supports for stdio transport). Stdio transport, not HTTP, for v1.0.
- **Tool naming:** lowercase dot-namespaced — `creek.state.read`, `creek.lint`, `creek.mine`, `creek.draft`, `creek.state.render` (the expensive re-render version). One-to-one with CLI commands; this becomes CrawDad's `intents` schema (FEAT-014).
- **Privacy-tier ceiling parameter:** every read tool takes a required `privacy_tier_ceiling` parameter (`open` | `personal` | `intimate` | `all`). Default is `open`. Returning content above the ceiling is impossible by construction; the tool returns title-only or refuses with a structured error.
- **Read tools in this FEAT:** `creek.state.read`, `creek.state.render`, `creek.lint`, `creek.mine`, `creek.draft`. Write tools (save, ingest, classify, link, report, skills) land in FEAT-011. Purge tools land in FEAT-012.
- **Audit log writes per tool call:** every MCP invocation appends to `00-Creek-Meta/audit/mcp.jsonl` — `{tool, args_summary, tier_ceiling, consumer, timestamp}`. Args-summary, not full args, to avoid leaking content into the audit log.
- **Tool input schemas:** documented JSON Schema per tool, generated from Pydantic models that wrap the CLI argument structure.

## Test plan

- Unit per tool wrapper.
- Integration: bootstrap the server via stdio in a subprocess, send a `tools/list` MCP message, verify the expected tools appear.
- Integration: call `creek.state.read` with `privacy_tier_ceiling=open` against a fixture vault containing `intimate` fragments — verify the response is title-only for those fragments.
- Regression: a tool call with `privacy_tier_ceiling=open` and a fragment tier of `intimate` returns the documented refusal status (not silent content leak).
- Regression: every tool call writes an audit log entry to `00-Creek-Meta/audit/mcp.jsonl`.
- Regression: the audit log entries contain `args_summary`, not full args (verified by an explicit "no full body in audit log" test against an `intimate`-tier draft request).

## Acceptance criteria

- `creek-tools-mcp` server starts via `python -m creek_mcp.server` or the entry point.
- Five read tools exposed and callable via the MCP protocol.
- `privacy_tier_ceiling` parameter is required on every read tool; tier-violations refuse rather than leak.
- Audit log writes happen on every tool call, with args-summary not full args.
- A registration recipe exists in `docs/mcp.md` for Claude Code (and notes that Claude Desktop / Cursor use the same `mcp.json`).
- ≥90% branch coverage on `creek_mcp/`.

## References

- Source candidate: ADAPT-004 (especially the privacy-by-construction enforcement at the MCP boundary).
- FEAT-014 (CrawDad's Haiku router consumes this server's tool registry as its `intents` schema).
- AlfredOS's Jig framing (MCP-as-capability-surface) is the prior art.
- Anthropic Python `mcp` SDK reference.
