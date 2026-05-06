# ADAPT-004: creek-tools MCP Server as CrawDad's Interface

**Verdict:** ADAPT
**Source system:** AlfredOS / Jig
**Affects:** Creek Vault data layer (exposes a new interface) + CrawDad agent layer (consumes it)
**Roadmap target:** v1 (the bridge that makes CrawDad possible)
**Estimated complexity:** M
**Conflicts with non-negotiables?** privacy — must be enforced (see "Translation")

## What it is

AlfredOS's [Jig framing](https://lumberjack.so/alfredos-10-is-here/) compiles workflow declarations into MCP servers, so any MCP-aware client (Claude Desktop, Cursor, Claude Code, Zed) can drive AlfredOS workflows through MCP tool-calls. The architectural choice: don't build a new client, slot into existing ones. Authoring once and exposing via MCP gives ambient capability across multiple agent surfaces.

For Creek, the analogous move is: don't have CrawDad shell out to the `creek` CLI. Don't have Claude Code shell out to the `creek` CLI either. Instead, expose creek-tools as an MCP server; both CrawDad and the developer's Claude Code consume the same surface.

## Why it's interesting

Three wins:

1. **Single source of truth for the tool surface.** Today there's a `creek` Typer CLI with 13 commands; tomorrow there's also CrawDad which needs to invoke them; meanwhile the developer uses Claude Code which currently has no structured way to drive `creek`. Without MCP, each consumer reinvents subprocess-spawning and argument-parsing. With MCP, one server, three consumers.
2. **Better privacy guarantees.** A subprocess interface is a process boundary, but argument-parsing is fragile and no consumer enforces privacy tier checks. An MCP server can enforce tier checks centrally — every tool call carries a tier, every tool checks it, no one can accidentally bypass.
3. **CrawDad becomes thin.** CrawDad's job is reduced to (a) Discord interaction, (b) Haiku-router intent extraction, (c) MCP tool dispatch, (d) Sonnet response composition. All the actual vault work lives in creek-tools, accessed via one cleanly-typed surface. The bot is small.

## Fit with Creek Vault and/or CrawDad

The MCP server lives in `creek-tools/` (or as a sibling package `creek-tools-mcp`). Tools mirror the existing CLI commands:

- `creek.process` (full pipeline)
- `creek.ingest` (with `--type` parameter)
- `creek.classify`, `creek.link`, `creek.report`, `creek.review`
- `creek.skills.generate`, `creek.mine`, `creek.draft`
- `creek.lint` (the new unified hygiene command from ADOPT-002)
- `creek.state` (the new audit-report command from ADOPT-005)
- `creek.save` (the file-back primitive from ADOPT-003)
- `creek.purge.*` family (with strong consent gating)

Tool inputs are JSON with documented schemas. Tool outputs are structured: status, summary, side-effects, references to written files, audit log entries.

CrawDad's Haiku router emits `intents` (ADOPT-008) where each intent's `type` matches an MCP tool name 1:1. The dispatcher just forwards to the MCP server.

Same MCP server is registered with the developer's Claude Code via standard MCP configuration. The developer drives the same operations through "do the lint pass and tell me what's surfaced" rather than running `creek lint` directly. Both CrawDad and Claude Code see the same tool surface.

## Translation if adapted

Three Creek-specific adaptations:

1. **Privacy tier is a first-class parameter on every read tool.** Every tool that returns vault content takes a `privacy_tier_ceiling` parameter (default: `open`). Returning content above the ceiling is impossible by construction; the tool returns title-only or refuses. This makes the privacy-tier system load-bearing at the MCP boundary rather than a downstream check.
2. **`creek.purge.*` requires elevated authorization.** The MCP server gates destructive operations behind a separate consent flow — perhaps an environment-variable token that's not given to CrawDad but is given to the developer's Claude Code. CrawDad cannot purge.
3. **Audit log writes for every tool call.** Every MCP invocation appends to `00-Creek-Meta/audit/mcp.jsonl` (consumer, tool, args summary, tier, timestamp). The audit trail is what makes "Geoff owns the artifacts" enforceable.

The MCP framing also disambiguates the relationship to skills: Creek's voice-skill tree (`creek-skills/`) is for the *LLM that drafts prose*, not for CrawDad. The MCP server is the interface; the skill tree is the voice-conditioning material that some MCP tools (like `creek.draft`) consume internally.

## Dependencies

- Pairs with: ADOPT-008 (Haiku-router intents map to MCP tools), ADAPT-003 (workflow DSL composes over MCP tools), ADOPT-003 (`creek.save` is the file-back tool).
- Reads: ADOPT-005 (`creek.state` is the audit-report tool).

## Acceptance criteria

- A `creek-tools-mcp` server is implemented (Python; mcp-server-sdk or equivalent) and exposes the existing CLI surface as tools with documented JSON schemas.
- Every tool that reads vault content takes a `privacy_tier_ceiling` parameter; tier-violations return refusal status, not content.
- `creek.purge.*` tools require elevated authorization and refuse calls without the right token.
- Every tool call writes an audit log entry to `00-Creek-Meta/audit/mcp.jsonl`.
- The MCP server can be registered with both Claude Code and a Discord bot framework.
- An end-to-end smoke test verifies that CrawDad → Haiku router → MCP dispatch → tool execution → response composition works for at least three intents (`creek.state`, `creek.mine`, `creek.draft`).
- The MCP server's tool registry is the source of truth for CrawDad's intent schema (ADOPT-008).
