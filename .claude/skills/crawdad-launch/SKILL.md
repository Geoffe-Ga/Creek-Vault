---
name: crawdad-launch
description: >-
  Launch the CrawDad Discord bot against a Creek vault. Use when the user asks
  to "launch CrawDad", "start the bot", "run CrawDad", "fire up the Discord
  bot", or otherwise bring the Discord-side interface online for a vault.
  ALWAYS ask which vault directory to run against first. Handles every launch
  gotcha (absolute vault_path, State/latest.md, voice-core mirroring, env
  secrets, allowlists) and starts the bot in the background. Do NOT use for
  driving the `creek` CLI / writing essays (use creek-cli) or editing
  crawdad/creek-tools source (use stay-green/work-issue).
---

# crawdad-launch

Brings the CrawDad Discord bot online against a chosen Creek vault. CrawDad is
the Discord-side interface to a vault; it spawns the `creek-tools-mcp` server
per turn and runs a Haiku-router → MCP → Sonnet-composer agent loop.

## Step 1 — Ask which vault (always)

Do **not** assume the vault. Ask the user for the vault directory. Offer the
known options as a sensible default:

- Demo / prod: `/Users/geoffgallinger/Documents/creek-demo-2026-05-30`
- Primary: `/Users/geoffgallinger/Documents/creek`

Run unattended against the **demo** vault unless told otherwise.

## Step 2 — Preflight + write the launch config

Run the bundled script with the chosen vault. It validates the vault, ensures
the env secrets, fixes the known gotchas, and writes `crawdad/crawdad.yaml`:

```bash
bash /Users/geoffgallinger/Projects/creek-tools/.claude/skills/crawdad-launch/launch.sh <VAULT>
```

What it checks/fixes (each is a real launch failure mode):
- **Env secrets** `DISCORD_BOT_TOKEN` + `ANTHROPIC_API_KEY` — the bot refuses to
  start without them. They are normally already in the shell env.
- **Absolute `vault_path`** in `<vault>/00-Creek-Meta/creek_config.yaml` — a
  relative path makes every MCP tool (`state.read`, `mine`, …) silently read the
  MCP process cwd instead of the vault. The script rewrites it to absolute.
- **`State/latest.md`** — runs `creek state` to generate it if missing (unblocks
  free-text replies + session-state load at startup).
- **`creek-skills/voice-core/SKILL.md`** — mirrors `meta/voice-core.SKILL.md`
  into it (bug #538) so replies sound like the vault owner, not generic.
- **Discord allowlists** — preserved from the existing config, else the known
  single-user defaults. Both lists must be non-empty or the bot won't start.

If the script prints `ERROR:`, stop and fix what it reports before launching.

## Step 3 — Launch the bot (background)

On `READY`, start the bot as a **background** Bash process (it's a long-running
Discord client, not a one-shot):

```bash
cd /Users/geoffgallinger/Projects/creek-tools/crawdad && \
  uv run crawdad run --config /Users/geoffgallinger/Projects/creek-tools/crawdad/crawdad.yaml
```

Use `run_in_background: true`. Then read the background output to confirm it
connected to Discord (look for the gateway/ready log line) and report the
channel it's listening in. Both `crawdad` and `creek-tools-mcp` run from source
via `uv run`, so **to pick up merged/edited code just restart the bot** — no
reinstall needed.

## Notes

- A running bot is frozen at launch-time code; restart after pulling new `main`.
- `crawdad.yaml` is trusted input (its `mcp_server_command` is executed verbatim
  as a subprocess argv) — keep it out of untrusted-writable directories.
- To stop the bot, kill the background process.
