# `creek-tools-mcp` registration guide (FEAT-010)

The `creek-tools-mcp` server exposes the read-only `creek` CLI surface
as MCP tools. Both the developer's Claude Code and the CrawDad Discord
bot (FEAT-013+) consume the same surface, so privacy-tier enforcement
and audit-log writes happen at one boundary.

## Installation

```bash
pip install -e .   # from creek-tools/
```

The entry point is registered by `pyproject.toml` as
`creek-tools-mcp`. The server speaks JSON-RPC over stdio; running it
interactively will appear to hang, which is correct.

## Tools (FEAT-010 part 1)

| Tool                  | Purpose                                                    |
|-----------------------|------------------------------------------------------------|
| `creek.state.read`    | Return the latest `00-Creek-Meta/State/latest.md` content. |
| `creek.state.render`  | Re-render the audit report (expensive).                    |

The follow-up PR adds `creek.lint`, `creek.mine`, and `creek.draft`.
FEAT-011 adds write tools; FEAT-012 adds `purge.*`.

Every tool requires a `privacy_tier_ceiling` parameter
(`open` | `personal` | `intimate` | `all`); default is `open`. Content
above the ceiling is omitted or returned as a title-only stub — the
ceiling cannot be bypassed by the caller.

## Claude Code

Add an entry to your project's `.mcp.json` (or to user-level
`~/.claude/mcp.json`):

```json
{
  "mcpServers": {
    "creek-tools": {
      "command": "creek-tools-mcp",
      "env": {"CREEK_MCP_CONSUMER": "claude-code"}
    }
  }
}
```

`CREEK_MCP_CONSUMER` is recorded on every audit-log entry so we can
tell apart calls from Claude Code, CrawDad, or operator-driven runs.

## Claude Desktop / Cursor / Zed

All three read the same `mcp.json` schema; the registration entry
above works verbatim. Drop it in the host's MCP config location.

## CrawDad

CrawDad (FEAT-013+) treats this server's tool registry as its
`intents` schema (FEAT-014). The Discord bot spawns the server as a
stdio child process. Set `CREEK_MCP_CONSUMER=crawdad` in the bot's
environment so the audit trail distinguishes Discord-driven calls.

## Audit log

Every tool invocation appends one entry to
`<vault>/00-Creek-Meta/audit/mcp.jsonl`:

```json
{
  "tool": "creek.state.read",
  "args_summary": {"vault_path": "/path/to/vault"},
  "tier_ceiling": "open",
  "consumer": "claude-code",
  "timestamp": "2026-05-11T12:34:56+00:00",
  "prev_hash": "..."
}
```

`args_summary` is compact: long strings become `{"len": N}`, lists
become `{"count": N}`, dicts become `{"keys": [...]}`. A draft request
for an `intimate`-tier fragment never leaks the body into the audit
trail. Hash chaining is provided by `creek.audit.AuditLog`; FEAT-012's
hardening pass extends the entry shape, not the storage layer.
