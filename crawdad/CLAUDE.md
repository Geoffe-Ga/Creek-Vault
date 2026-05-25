# Claude Code Project Context: crawdad

CrawDad is a Discord bot that consumes the creek-tools MCP surface.
It is a sibling of `creek-tools/`, not nested under it (FEAT-013
§Pre-decided choices).

## 1. Critical principles

These mirror `creek-tools/CLAUDE.md` at a smaller scale:

1. **Use project scripts, not direct tools.** Always invoke
   `./scripts/*.sh` rather than calling `ruff`, `mypy`, or `pytest`
   directly. Scripts ensure CI parity.
2. **No shortcuts.** Never add `# type: ignore`, `# noqa`, or
   `--no-verify` without an issue reference. Fix root causes.
3. **Stay green.** Never push, request review, or merge with red checks.
   See [4. Stay Green Workflow](#4-stay-green-workflow).
4. **Quality bar is non-negotiable.**
   - Branch coverage ≥ 90%
   - Docstring coverage ≥ 95% (`interrogate`)
   - Cyclomatic complexity ≤ 10 per function (Xenon `--max-absolute B`)
   - MyPy strict mode, zero violations
   - Ruff lint + format, zero violations
   - Bandit, zero medium-or-above findings

## 2. Project overview

CrawDad is the Discord-side interface to a Creek vault. v1.0 is
complete and ships:

- Connection to `creek-tools-mcp` (FEAT-010/011/012) over stdio.
- The two-LLM agent loop: Haiku router (FEAT-014) → MCP dispatcher →
  Sonnet composer (FEAT-015), capped at 5 rounds with paradox auto-
  routing to `10-Liminal/Paradoxes/`.
- Voice-skill activation per session from `<vault>/creek-skills/`
  (voice-core + phase + register).
- Seven `/crawdad` Discord slash commands (FEAT-016 + FEAT-029 +
  ADAPT-003): `reflect`, `checkin`, `surface`, `draft`, `save`,
  `register`, `workflow`. `workflow` supports `list` (enumerate) and
  `run <name>` (deterministic walk over a YAML file) — see
  `crawdad/crawdad/workflows.py` and
  `docs/adr/2026-05-24_workflow-file-location.md`.
- A user + channel allowlist; non-allowlisted users get *no* response
  (silent ignore, per FEAT-013's personal-use scoping).
- Session-state load from `<vault>/00-Creek-Meta/State/latest.md` at
  startup.

## 3. Layout

```
crawdad/
├── crawdad/                # Source package
│   ├── __init__.py
│   ├── bot.py              # discord.Client subclass + pure-logic handler
│   ├── cli.py              # `crawdad run` entry point + agent components
│   ├── composer.py         # Sonnet composer (FEAT-015)
│   ├── config.py           # Pydantic config (env secrets + YAML file)
│   ├── dispatcher.py       # Intent → MCP tool dispatcher (FEAT-014)
│   ├── history.py          # Conversation history with bounded truncation
│   ├── intents.py          # Pydantic intent schema + builder
│   ├── loop.py             # 5-round agent loop (FEAT-015)
│   ├── mcp_client.py       # async MCP stdio wrapper
│   ├── router.py           # Haiku intent router (FEAT-014)
│   ├── skill_loader.py     # Voice-skill stack loader (FEAT-015)
│   ├── slash_commands.py   # /crawdad Discord slash commands (FEAT-016)
│   ├── state.py            # load_session_state — latest.md parser
│   ├── workflows.py        # Authored workflow DSL (ADAPT-003)
│   └── builtin_workflows/  # Reference workflows shipped with the package
├── tests/                  # One test file per source module
├── scripts/                # check-all, lint, test, etc.
├── docs/
├── CLAUDE.md
├── README.md
└── pyproject.toml
```

## 4. Stay Green workflow

Four gates, each must pass before the next:

1. **TDD.** Write the failing test first. Tests pin acceptance criteria.
2. **Local checks.** `./scripts/check-all.sh` exits 0.
3. **CI.** All jobs green on the PR.
4. **Review.** LGTM with no reservations.

## 5. Configuration

- Secrets ONLY via env: `DISCORD_BOT_TOKEN`, `ANTHROPIC_API_KEY`.
- Everything else in `crawdad.yaml`:
  - `vault_path` — Obsidian vault root.
  - `mcp_server_command` — argv for the MCP subprocess (default
    `[creek-tools-mcp]`).
  - `allowed_user_ids` — Discord user ids that may message the bot.
  - `allowed_channel_ids` — channels the bot will respond in.

Both allowlists must be non-empty — an empty list is a configuration
error, not "open to everyone".

### 5.1 Trust boundary

`crawdad.yaml` is **trusted input**. `mcp_server_command` is executed
verbatim as a subprocess argv, so anyone with write access to
`crawdad.yaml` can run arbitrary code as the bot user. For the
single-user / personal-tool deployment target this is acceptable, but
it means:

- The YAML file lives wherever the operator runs the bot — keep it out
  of any directory writable by less-trusted processes.
- Do *not* feed user-controlled content into `crawdad.yaml`.
- Multi-user / shared deployments are out of scope for v1.0; revisit
  this assumption before broadening access.

## 6. MCP subprocess resilience

The bot does **not** exit on MCP subprocess failure. The pattern is:

1. `MCPClient(config.mcp_server_command).connect()` is an async
   context manager. SDK failures surface as `MCPUnavailableError`.
2. The handler catches `MCPUnavailableError` and replies with the
   documented soft error (`creek-tools is unreachable; try again in a
   moment.`).
3. FEAT-014's dispatcher will add the exponential-backoff restart loop
   (capped at 3 retries / 30s) on top of this skeleton.

## 7. Quality thresholds (mirrors creek-tools)

| Gate | Threshold | Tool |
|---|---|---|
| Branch coverage | ≥ 90% | `pytest --cov-fail-under=90` |
| Docstring coverage | ≥ 95% | `interrogate --fail-under=95` |
| Cyclomatic complexity | ≤ 10 / function | `xenon --max-absolute B` |
| Type checking | strict, zero | `mypy --strict` |
| Lint + format | zero | `ruff check` + `ruff format` |
| Security | zero medium+ | `bandit -r crawdad/ -ll` |
