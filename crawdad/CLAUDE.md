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

CrawDad is a Discord bot:

- Connects to `creek-tools-mcp` (FEAT-010) over stdio.
- Reads `<vault>/00-Creek-Meta/State/latest.md` once at session start
  (Graphify-style PreToolUse pattern).
- Enforces a hard-coded user/channel allowlist — non-allowlisted users
  get *no* response (silent ignore, per FEAT-013's personal-use
  scoping).
- Posts a stub reply for the wiring scaffold. FEAT-014 will swap the
  stub for the Haiku-router + dispatcher; FEAT-015 will add the Sonnet
  composer and 5-round loop.

## 3. Layout

```
crawdad/
├── crawdad/                # Source package
│   ├── __init__.py
│   ├── bot.py              # discord.Client subclass + pure-logic handler
│   ├── cli.py              # `crawdad run` entry point
│   ├── config.py           # Pydantic config (env secrets + YAML file)
│   ├── mcp_client.py       # async MCP stdio wrapper
│   └── state.py            # load_session_state — latest.md parser
├── tests/
│   ├── conftest.py
│   ├── fixtures/
│   │   └── fake_mcp_server.py   # stdio MCP server used by client tests
│   ├── test_bot.py
│   ├── test_cli.py
│   ├── test_config.py
│   ├── test_mcp_client.py
│   └── test_state.py
├── scripts/
│   ├── check-all.sh        # Run every quality gate
│   ├── coverage.sh
│   ├── format.sh
│   ├── lint.sh
│   ├── security.sh
│   ├── test.sh
│   └── typecheck.sh
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
