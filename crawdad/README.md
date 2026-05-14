# CrawDad

CrawDad is the Discord-side interface to a Creek vault. It consumes
the creek-tools MCP surface (see `creek-tools/creek_mcp`) and answers
Discord messages in your voice.

This is the **FEAT-013 wiring scaffold** — the agent loop (FEAT-014:
Haiku router + dispatcher; FEAT-015: Sonnet composer + 5-round loop)
is not yet implemented. What ships in this PR:

- A `discord.py` client that connects to Discord and forwards messages
  to a pure-logic handler.
- An async MCP stdio client wrapping the Anthropic `mcp` SDK.
- A `latest.md` parser that loads the audit-report snapshot once at
  session start.
- A user + channel allowlist; non-allowlisted callers get no response.
- A graceful "creek-tools is unreachable" reply when the MCP
  subprocess dies. FEAT-014 will add the exponential-backoff restart
  loop on top.

## Quick start

```bash
cd crawdad
pip install -e ".[dev]"

# Required env vars
export DISCORD_BOT_TOKEN="…"
export ANTHROPIC_API_KEY="…"

# Edit crawdad.yaml — see the example below
crawdad run --config ./crawdad.yaml
```

## `crawdad.yaml` example

```yaml
vault_path: /absolute/path/to/your/vault
mcp_server_command:
  - creek-tools-mcp
allowed_user_ids:
  - 123456789012345678   # the developer's Discord user id
allowed_channel_ids:
  - 234567890123456789   # the channel the bot will respond in
```

## Development

```bash
./scripts/check-all.sh      # every quality gate
./scripts/test.sh           # unit tests
./scripts/lint.sh --fix     # auto-fix
./scripts/typecheck.sh
```

The quality bar mirrors `creek-tools/`: ≥ 90 % branch coverage, MyPy
strict clean, Ruff zero violations, ≥ 95 % docstring coverage,
cyclomatic complexity ≤ 10.

## Architecture

```
Discord message
  ▼
CrawDadClient.on_message  (crawdad/bot.py)
  ▼
handle_message            (pure logic, allowlist + state dispatch)
  ▼
session_state             (loaded once at start from latest.md)
  ▼
[FEAT-014 dispatcher]     ── MCP subprocess (creek-tools-mcp)
  ▼
[FEAT-015 composer]
```

## See also

- `creek-tools/CLAUDE.md` — quality standards we mirror.
- `creek-tools/docs/mcp.md` — the MCP surface CrawDad consumes.
- `plans/git-issues/FEAT-013-crawdad-discord-bot-skeleton-mcp-client.md`
  — the issue this package implements.
