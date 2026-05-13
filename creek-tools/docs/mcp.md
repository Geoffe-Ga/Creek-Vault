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

## Tools

### Read tools (FEAT-010)

| Tool                  | Purpose                                                    |
|-----------------------|------------------------------------------------------------|
| `creek.state.read`    | Return the latest `00-Creek-Meta/State/latest.md` content. |
| `creek.state.render`  | Re-render the audit report (expensive).                    |
| `creek.lint`          | Run the unified hygiene lint pass (FEAT-008).              |
| `creek.mine`          | Surface essay seeds from the compiled vault layer.         |
| `creek.draft`         | Draft an essay from a mined idea (requires an LLM).        |

### Write tools (FEAT-011)

| Tool                    | Inputs                                                      | Write-side tier rule                                              |
|-------------------------|-------------------------------------------------------------|-------------------------------------------------------------------|
| `creek.save`            | `target`, `body`, `title?`, `tier`, `provenance?`           | Caller's `ceiling` must admit `tier` — intimate via open refused. |
| `creek.ingest`          | `source_type`, `input_path`                                 | Default ingest tier is `personal`; `ceiling=open` is refused.     |
| `creek.classify`        | `method` (`rules`\|`llm`), `force`                          | Rewrites in place; no new tier produced — any ceiling permitted.  |
| `creek.link`            | `method` (`embeddings`\|`temporal`\|`eddies`), `rebuild`    | Links existing artefacts in place — any ceiling permitted.        |
| `creek.report`          | `report_type` (`tags`\|`voice`)                             | Renders a vault-state report — any ceiling permitted.             |
| `creek.skills.refresh`  | none beyond `ceiling`                                       | Voice-skill tree regen; intimate exemplars already excluded.      |
| `creek.compile`         | `fragment_ids`, `target_kind`, `target_id`, `target_title`  | Idempotent per FEAT-003; no-op re-runs do not log a duplicate.    |

FEAT-012 adds `purge.*` and consent-elevated paths.

Every tool requires a `privacy_tier_ceiling` parameter
(`open` | `personal` | `intimate` | `all`); default is `open`. Note
that `open` is the *most restrictive* setting — it restricts the
caller to open-tier (publishable) content only, not "open access".
The ladder goes `open` (publishable) → `personal` (summarised
personal allowed) → `intimate` (everything self-authored) → `all`
(every tier including unclassified). Content above the ceiling is
omitted or returned as a title-only stub — the ceiling cannot be
bypassed by the caller.

### Write-side tier-ceiling rule (FEAT-011)

A write tool that would *create* content at tier `T` requires the
caller's `privacy_tier_ceiling` to admit `T`. So `creek.save` with
`ceiling=open` and `tier=intimate` is refused with `status="refused"`
rather than silently downgraded — the body never lands in the vault
and the audit entry records the refusal without the body. The same
gate applies to `creek.ingest` because the default ingestor tier is
`personal`.

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
  "tool": "creek.mine",
  "args_summary": {"phase": "rising", "limit": 10},
  "tier_ceiling": "open",
  "consumer": "claude-code",
  "timestamp": "2026-05-11T12:34:56+00:00",
  "prev_hash": "..."
}
```

`args_summary` captures the MCP-supplied arguments to the tool (the
ceiling is already a top-level field, so it's not duplicated here).
The vault path is *not* recorded — it's resolved internally from
`load_config()` and never enters the audit trail. Compact summary
rules: long strings become `{"len": N}`, lists become `{"count": N}`,
dicts become `{"keys": [...]}`. A draft request for an
`intimate`-tier fragment never leaks the body into the audit
trail. Hash chaining is provided by `creek.audit.AuditLog`; FEAT-012's
hardening pass extends the entry shape, not the storage layer.

#### Write-tool audit fields (FEAT-011)

Write-tool entries add three optional fields on top of the read-tool
schema:

```json
{
  "tool": "creek.save",
  "args_summary": {"target": "thread", "tier": "open", "body": {"len": 4096}},
  "tier_ceiling": "open",
  "consumer": "crawdad",
  "timestamp": "2026-05-12T12:34:56+00:00",
  "created_path": "02-Threads/Active/2026-05-12-saved-thread.md",
  "created_tier": "open",
  "affected_fragment_ids": ["frag-a", "frag-b"]
}
```

`created_path` is the relative path of the produced file (or the
container directory for batch tools like `creek.skills.refresh`);
`created_tier` is the tier the content was written at;
`affected_fragment_ids` is an ID list — never fragment bodies. Tools
that update artefacts **in place** (`creek.classify`, `creek.link`) do
not produce new files, so they omit `created_path` and `created_tier`
from their audit entry.

For tools that accept a `body` argument (`creek.save`), the audit entry
records `body_len` rather than `body` — on both the success and the
refusal path — so a fragment body never lands in `mcp.jsonl` verbatim,
regardless of length.

`creek.compile` skips the audit append on no-op re-runs (idempotent
per FEAT-003) — the engine still runs (the LLM call is not yet
skipped), but the audit log does not grow when a re-compile produces
an identical target page.

## Troubleshooting

- **Server appears to hang:** correct. It speaks JSON-RPC over stdio.
  Use `python -m creek_mcp.server` for the same effect.
- **`No module named "mcp"`:** install with `pip install -e .` — FEAT-010
  added the `mcp` SDK to `pyproject.toml`.
- **`creek.draft` returns "LLM provider unavailable":** the server
  loads the LLM lazily so only `draft` requires it. Configure
  `ANTHROPIC_API_KEY` or a running Ollama instance.
- **Fewer mine seeds than expected:** the ceiling is filtering intimate
  fragments by design. Raise the ceiling to `intimate` or `all` only
  when the caller is authorised.
