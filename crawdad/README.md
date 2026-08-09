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
- A user + channel allowlist; non-allowlisted callers get no response
  — and, as of #1052, no bot-capture record either (see
  [Bot capture](#bot-capture) below).
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
| `capture_enabled` | no | `False` | Bot-capture toggle (#687) — see [Bot capture](#bot-capture) below. |
| `capture_subpath` | no | `discord-capture` | Vault-relative dir bot-capture writes into. Must stay inside the vault. |

Secrets are **never** in `crawdad.yaml` — they come only from the environment.

### Bot capture

Bot-capture logs the channels CrawDad is in to `<vault>/<capture_subpath>/`
for later ingest — it is off by default (`capture_enabled: false`). When on,
the bot captures **only messages it would also respond to**: the same
user + channel allowlists as the command path, never another bot or
webhook, and never its own replies.

It also **never** captures a channel declared `intimate` or `all` in
`attachments.channel_privacy_tiers` — those channels still get normal
`/crawdad` and free-text replies, they are just never logged to the
vault, because a captured message currently can't carry its channel's
privacy tier with it. #1262 tracks carrying that tier end-to-end so
`intimate` channels can be captured faithfully instead of refused.

**Upgrading from a pre-#1052 build.** If you ran with
`capture_enabled: true` before this fix, `<vault>/discord-capture/`
may already hold records from non-allowlisted users, other bots, or
an `intimate` channel — the old capture path bypassed all three
gates. These records are still ingestible today. #1264 tracks the
fix; until then the manual mitigation is deleting the offending
channel subdirectories under `discord-capture/` before running
`creek sync`.

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
| `/crawdad workflow [list\|run <name>]` | List registered workflows, or walk one. See [Workflow files](#workflow-files). |

Full grammar reference (including the developer-side `/creek` surface
for Claude Code) is in
[`creek-tools/docs/slash-commands.md`](../creek-tools/docs/slash-commands.md).

### Workflow files

`/crawdad workflow run <name>` executes a `*.workflow.yaml` file: a
deterministic, router-free walk over an ordered list of MCP tool
calls (DSL reference — interpolation syntax, per-step error handling —
is the module docstring in `crawdad/crawdad/workflows.py`). Files are
discovered from two sources, in order, a user `name` beating a
built-in of the same name: `crawdad/crawdad/builtin_workflows/`
(shipped, version-controlled) and `<vault>/00-Creek-Meta/Workflows/`
(your own, outside version control — see
`crawdad/docs/adr/2026-05-24_workflow-file-location.md`).

| Key | Required | Default | Notes |
|---|---|---|---|
| `name` | yes | — | Unique id; the argument to `workflow run <name>`. |
| `description` | yes | — | Shown by `/crawdad workflow list`. |
| `trigger` | no | — | Documentation only; not read by the walker. |
| `phase_aware` | no | `false` | If `true`, refuses to run with no session phase. |
| `allowed_phases` | no | any | Non-empty list narrows `phase_aware` to specific phases. |
| `privacy_tier_ceiling` | no | `open` | See below. |
| `inputs` | no | — | Names the caller must supply, or the run is refused. |
| `steps` | yes | — | Ordered `{id, tool, args}` list; one MCP tool call each. |

**The ceiling, ordered least-to-most permissive:** `open` <
`personal` < `intimate` < `all` — despite its name, `open` is the
*most restrictive* tier, not "no restriction"; that inversion is what
the old key name got backwards. Only `open`/`personal` may be
declared: a workflow requesting `intimate` or `all` still parses and
still shows up in `/crawdad workflow list`, but `run` refuses it in
Discord at run time, because every tool result is relayed to a cloud
LLM composer and posted straight into a Discord message.

**Renamed from `privacy_tier_floor`**, which was inverted (raising the
"floor" widened access). The old key still works, value-preserving,
with a `WARNING` naming the workflow (removal tracked in
[#1151](https://github.com/Geoffe-Ga/Creek-Vault/issues/1151)); both
keys in one file is a parse error. For an existing file:
`open`/`personal` values run unchanged —
rename at your leisure; `intimate`/`all` values now get refused at
`run` (intended — that value was granting intimate reads, never
requiring intimate protection); both keys present fails to parse and
the workflow drops out of the listing.

**No per-step ceilings** — a step's `args:` may not set its own
`privacy_tier_ceiling` (or the deprecated `privacy_tier_floor`); the
workflow-level value applies to every step, and a step that tries is
refused at run time, naming the step.

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
