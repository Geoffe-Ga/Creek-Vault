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
metadata:
  author: Geoff
  version: 1.1.0
---

# crawdad-launch

Brings the CrawDad Discord bot online against a chosen Creek vault. CrawDad is
the Discord-side interface to a vault; it spawns the `creek-tools-mcp` server
per turn and runs a Haiku-router → MCP → Sonnet-composer agent loop.

## Step 1 — Ask which vault (always)

Always ask the user which vault directory to launch against — never assume one.
To help them choose:

- **Discover the Creek vaults on this machine** (a vault is any dir with a
  `00-Creek-Meta/creek_config.yaml`):
  ```bash
  find ~ -maxdepth 5 -path '*/00-Creek-Meta/creek_config.yaml' 2>/dev/null \
    | sed 's#/00-Creek-Meta/creek_config.yaml##'
  ```
- **Offer the last-launched vault as the default.** If `crawdad/crawdad.yaml`
  already exists, its `vault_path` is the vault CrawDad last ran against — a
  sensible default when the user has no preference. Read it with:
  ```bash
  sed -n "s/^vault_path:[[:space:]]*//p" "$(git rev-parse --show-toplevel)/crawdad/crawdad.yaml" | tr -d "\"'"
  ```

If a non-interactive run gives you no vault and no prior config exists, stop and
ask rather than guessing.

## Step 2 — Preflight + write the launch config

Run the bundled script with the chosen vault. It validates the vault, ensures
the env secrets, fixes the known gotchas, and writes `crawdad/crawdad.yaml`.
Resolve the script via the repo root so this works on any checkout:

```bash
bash "$(git rev-parse --show-toplevel)/.claude/skills/crawdad-launch/launch.sh" <VAULT>
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
- **Discord allowlists** — all ids preserved from the existing `crawdad.yaml`,
  else taken from `CRAWDAD_DEFAULT_USER` / `CRAWDAD_DEFAULT_CHANNEL` in the env.
  A fresh vault with no prior config and no env defaults fails with a clear
  message (both lists must be non-empty or the bot won't start) — export those
  two vars or hand-edit the allowlists before retrying.
- **MCP launch wrapper** — `mcp_server_command` is generated as a **login shell**
  wrapper (`$SHELL -lc 'export CREEK_ANTHROPIC_CONSENT=1; exec <venv>/creek-tools-mcp …'`)
  rather than a bare `uv run`. This fixes two real failures: (1) CrawDad spawns
  the MCP server through the stdio SDK, which **scrubs the environment** and drops
  `ANTHROPIC_API_KEY`, so in-MCP cloud tools (`draft`/`author`/`mine`/`classify`)
  silently degrade — the login shell re-establishes the key from your profile;
  and (2) spawning `uv run` every turn is slow and contends on uv's project lock
  across agent-loop rounds, surfacing as `could not start … ('uv')` →
  `mcp_unavailable` replies — `exec`ing the creek-tools venv entry point directly
  is fast, lock-free, and keeps the JSON-RPC stdout stream clean. Falls back to
  `uv run` if the venv binary isn't built yet (`uv sync` in `creek-tools`).
  **`CREEK_ANTHROPIC_CONSENT=1` means vault content egresses to Anthropic on the
  user's key** when those tools run — intended for the user's own vault.
  Because the login shell only sources *login* profiles (`~/.zprofile`,
  `~/.zshenv`, `~/.bash_profile`, `~/.profile`) and **not** interactive rc files
  (`~/.zshrc`, `~/.bashrc`), `ANTHROPIC_API_KEY` must be exported in a login
  file. The script probes for this under a scrubbed env and prints a `[warn]`
  (with the fix) if the key is only in an rc file — heed it or the cloud tools
  degrade exactly as before.

If the script prints `ERROR:`, stop and fix what it reports before launching.

## Step 3 — Launch the bot (background)

On success the script prints a `READY` block ending with the exact, fully
resolved launch command under `Launch with:`. Run **that** command verbatim
(it already contains the right repo-derived absolute paths — don't hand-write
paths) as a **background** Bash process, since it's a long-running Discord
client, not a one-shot. The shape is:

```bash
cd <REPO>/crawdad && uv run crawdad run --config <REPO>/crawdad/crawdad.yaml
```

Use `run_in_background: true`. Then read the background output to confirm it
connected to Discord (look for the gateway/ready log line) and report the
channel it's listening in. `crawdad` runs from source via `uv run`, and
`creek-tools-mcp` runs from its pre-built editable venv binary (or `uv run` in
the fallback) — both reflect source edits, so **to pick up merged/edited code
just restart the bot**, no reinstall needed. Only a *dependency* change needs a
`uv sync` in `creek-tools` first.

## Notes

- A running bot is frozen at launch-time code; restart after pulling new `main`.
- `crawdad.yaml` is trusted input (its `mcp_server_command` is executed verbatim
  as a subprocess argv) — keep it out of untrusted-writable directories.
- To stop the bot, kill the background process.
