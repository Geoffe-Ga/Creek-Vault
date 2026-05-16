# CrawDad

CrawDad is the Discord-side interface to a Creek vault. It consumes
the creek-tools MCP surface (see `creek-tools/creek_mcp`) and answers
Discord messages in your voice using the FEAT-015 agent loop (Haiku
router → MCP dispatcher → Sonnet composer with voice-skill activation).

CrawDad v1.0 ships:

- A `discord.py` client that connects to Discord and forwards messages
  to a pure-logic handler.
- The two-LLM agent loop (FEAT-014 + FEAT-015) — Haiku for intent
  extraction, Sonnet for voice-faithful composition, capped at 5
  rounds with paradox routing to `10-Liminal/Paradoxes/`.
- An async MCP stdio client wrapping the Anthropic `mcp` SDK.
- Voice-skill activation per session from `<vault>/creek-skills/`.
- The six `/crawdad` slash commands (FEAT-016): `reflect`, `checkin`,
  `surface`, `draft`, `save`, `workflow`.
- A user + channel allowlist; non-allowlisted callers get no response.
- A graceful "creek-tools is unreachable" reply when the MCP
  subprocess dies.

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

## Slash commands (FEAT-016)

The `/crawdad` family registers automatically with Discord on startup.
Each routes through the FEAT-015 agent loop with a pre-baked user
message, so the Sonnet composer wraps the tool results in your voice.

| Command | Purpose |
|---|---|
| `/crawdad reflect` | Open reflective conversation mode — the loop with no preselected intent. Same as bare `/crawdad`. |
| `/crawdad checkin` | Wavelength check-in via `creek.state.read`. |
| `/crawdad surface` | Surface paradoxes / liminal content via `creek.lint`. |
| `/crawdad draft <topic>` | Mine + draft on the supplied topic (`creek.mine` → `creek.draft`). |
| `/crawdad save <content>` | File the supplied content back to the vault (`creek.save`). |
| `/crawdad workflow [list]` | v1.0 stub — full workflow DSL ships in v1.1. |

Full grammar reference (including the developer-side `/creek` surface
for Claude Code) is in
[`creek-tools/docs/slash-commands.md`](../creek-tools/docs/slash-commands.md).

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
Discord message  /  /crawdad slash command
  ▼
CrawDadClient.{on_message, /crawdad <cmd>}  (crawdad/bot.py + slash_commands.py)
  ▼
handle_message  /  loop_runner closure
  ▼
loop.run_one_turn   (FEAT-015 agent loop, capped at 5 rounds)
  ▼  ┌──────────────────────────────────────────────────────────┐
     │ Haiku router (FEAT-014)   →   JSON intents               │
     │ MCP dispatcher            →   creek.state / lint / ...   │
     │ Paradox auto-save         →   creek.save → 10-Liminal/   │
     │ Sonnet composer (FEAT-015)→   voice-faithful reply       │
     └──────────────────────────────────────────────────────────┘
  ▼
Discord reply
```

The voice-skill stack (`<vault>/creek-skills/voice-core/`, plus phase-
and register-specific files) is loaded once at session start. See
`crawdad/CLAUDE.md` §5.1 for the trust boundary on `crawdad.yaml`.

## See also

- `creek-tools/CLAUDE.md` — quality standards we mirror.
- `creek-tools/docs/mcp.md` — the MCP surface CrawDad consumes.
- `creek-tools/docs/slash-commands.md` — full `/creek` + `/crawdad` grammar.
- `plans/git-issues/FEAT-013-crawdad-discord-bot-skeleton-mcp-client.md`,
  `plans/git-issues/FEAT-014-crawdad-haiku-router-dispatcher.md`,
  `plans/git-issues/FEAT-015-crawdad-sonnet-composer-loop.md`,
  `plans/git-issues/FEAT-016-slash-command-grammar.md` — the
  implementation plan.
