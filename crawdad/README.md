# CrawDad

CrawDad is the Discord-side interface to a Creek vault. It consumes
the creek-tools MCP surface (see `creek-tools/creek_mcp`) and answers
Discord messages in your voice using the FEAT-015 agent loop (Haiku
router → MCP dispatcher → Sonnet composer with voice-skill activation).

CrawDad v1.0 ships:

- A `discord.py` client that connects to Discord and forwards messages
  to a pure-logic handler.
- The two-LLM agent loop (FEAT-014 + FEAT-015) — Haiku for intent
  extraction, Sonnet for voice-faithful composition, capped at
  `MAX_LOOP_ROUNDS` (default 5; operator-configurable via
  `crawdad.yaml::max_loop_rounds`, bounded `[1, 50]` — FEAT-036) with
  paradox routing to `10-Liminal/Paradoxes/`.
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
export ANTHROPIC_API_KEY="…"          # the key for the selected provider (default: anthropic)

# Optional: pick a different backend (see "LLM provider selection" below)
# export CRAWDAD_PROVIDER=openai      # then set OPENAI_API_KEY instead
# export CRAWDAD_PROVIDER=gemini      # then set GOOGLE_API_KEY instead

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
# Optional FEAT-036 knob: raise the agent-loop round cap when a single
# user turn legitimately needs more router/dispatcher passes (multi-
# source ingest, large re-classification asks, long workflow chains).
# Default 5; bounded [1, 50] — the upper bound prevents pathological
# runaway loops. Omit the key to keep the v1 default.
# max_loop_rounds: 12
```

### Configuration keys

| Key | Required | Default | Notes |
|---|---|---|---|
| `vault_path` | yes | — | Absolute path to the Obsidian vault root. |
| `mcp_server_command` | no | `[creek-tools-mcp]` | argv for the MCP subprocess. |
| `allowed_user_ids` | yes | — | Discord user ids permitted to message the bot. |
| `allowed_channel_ids` | yes | — | Channels the bot will respond in. |
| `attachments` | no | see source | Per-attachment limits, allow/deny lists, channel privacy tiers (FEAT-027/035). |
| `consent` | no | see source | Conversational consent tokens + TTL (FEAT-034). |
| `max_loop_rounds` | no | `5` | FEAT-036 agent-loop round cap, bounded `[1, 50]`. Raise when a single user turn legitimately needs more router/dispatcher passes (multi-source ingest, large re-classification asks, long workflow chains); the upper bound prevents pathological runaway loops. |

Secrets are **never** in `crawdad.yaml` — they come only from the environment.

### LLM provider selection

CrawDad's backend is chosen by the `CRAWDAD_PROVIDER` environment variable (default `anthropic`). The selected provider's API key must be present in the environment; each SDK reads its own key — CrawDad **never** stores a key on its config object or in `crawdad.yaml`.

| `CRAWDAD_PROVIDER` | Required env key |
|---|---|
| `anthropic` (default) | `ANTHROPIC_API_KEY` |
| `openai` | `OPENAI_API_KEY` |
| `gemini` | `GOOGLE_API_KEY` |

Starting the bot with the selected provider's key unset fails fast with a message naming the missing variable. Per-provider router/composer model tiers live in `crawdad/config.py`; override globally with `CRAWDAD_ROUTER_MODEL` / `CRAWDAD_COMPOSER_MODEL`.

CrawDad's provider abstraction is intentionally **decoupled** from creek-tools' (sibling packages, no shared module) — see [ADR-0003](../creek-tools/docs/architecture/ADR/0003-decoupled-provider-abstractions.md).

**Live smoke test (model onboarding).** Unit tests mock every vendor SDK, so a model tier is only proven by a real call. With the provider's key in the env, one command makes a single tiny live request and asserts the normalized round-trip:

```bash
./scripts/test.sh -m integration -k openai                                       # smoke the composer's default tier
CRAWDAD_COMPOSER_MODEL=some-new-model ./scripts/test.sh -m integration -k gemini # smoke a candidate model id
```

(`test.sh` injects `--no-cov` for integration runs so the project-wide coverage gate doesn't bury the smoke output.)

Each smoke skips cleanly when its key is absent; the default `./scripts/test.sh` run deselects the `integration` marker, so CI never makes (or bills) a live call.

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
loop.run_one_turn   (FEAT-015 agent loop, capped at max_loop_rounds; default 5)
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
