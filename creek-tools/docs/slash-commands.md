# Slash commands (`/creek` and `/crawdad`)

FEAT-016 ships two small slash-command surfaces that share a grammar:
`/creek` for Claude Code, `/crawdad` for Discord. Both invoke the same
MCP tool surface, so the two front doors lead to one system.

## Why two surfaces?

- **`/creek`** — the developer's keyboard in Claude Code. Ten commands,
  one per `creek` primitive. Calls go through the `creek-tools-mcp`
  server (stdio).
- **`/crawdad`** — the user's conversation in Discord. Six commands,
  each entering the FEAT-015 agent loop with a pre-baked user message
  so Sonnet composes the reply in your voice. The loop ultimately
  calls the same MCP tools.

## `/creek <subcommand>` (Claude Code)

Each subcommand is a markdown skill under
`creek-tools/.claude/commands/<name>.md`. Claude Code reads the file
body as the prompt when you type `/creek <name>`.

| Command | MCP tool | Purpose |
|---|---|---|
| `/creek` (no args) | `creek.state.read` | Default: render the latest vault state. |
| `/creek state [--render]` | `creek.state.read` or `creek.state.render` | Audit report; `--render` forces regeneration. |
| `/creek lint [--checks ...] [--since DATE]` | `creek.lint` | Vault hygiene + drift + paradox surfacing. |
| `/creek mine [--phase ...] [--limit N]` | `creek.mine` | Surface essay seeds. |
| `/creek draft [--phase ...] [--index N]` | `creek.draft` | Generate a voice-faithful draft from a mined seed. |
| `/creek save --target ...` | `creek.save` | File an answer / paradox / draft / liminal item. |
| `/creek explain [SUBCOMMAND]` | none (help) | List commands or render one in detail. |
| `/creek phase` | `creek.state.read` | Shorthand for the wavelength snapshot only. |
| `/creek wavelength` | `creek.state.read` | Alias for `/creek phase`. |
| `/creek skills [--refresh]` | `creek.skills` / `creek.skills.refresh` | Inspect or regenerate the voice-skill tree. |
| `/creek ingest --type ... --input ...` | `creek.ingest` | Run the ingestion pipeline on a new source. |

### `report` types

`creek report --type <type>` (MCP: `creek.report`) supports `tags`, `voice`,
`unnamed`, `wavelength`, `fingerprint`, `lexicon`, `decisions`, and
`rhetorical-patterns`:

- `lexicon` — persists the voice glossary + metaphor index to
  `07-Voice/Lexicon/`.
- `decisions` — writes draft Decision notes from decision-signalling fragments
  to `08-Decisions/Active/` (idempotent: a fragment already captured is
  skipped).
- `rhetorical-patterns` — writes a per-register rhetorical-moves note to
  `07-Voice/Rhetorical-Patterns/`.
- `mode-profiles` — writes a per-engagement-mode profile to
  `05-Wavelength/Mode-Profiles/`.

### Discoverability

`/creek explain` lists every subcommand. Bare `/creek` runs the default
(state report). Each command file documents its own purpose, so
help-at-every-prefix works by reading the file Claude Code already
ships in the prompt.

## `/crawdad <subcommand>` (Discord)

Registered as Discord application slash commands. Each routes through
the FEAT-015 agent loop: the slash command builds a pre-formulated
user message and runs it through `loop.run_one_turn`. The Sonnet
composer (FEAT-015) wraps the tool results in your voice.

| Command | Behaviour |
|---|---|
| `/crawdad` (no args) | Open reflective conversation mode — the FEAT-015 loop with no preselected intents. |
| `/crawdad reflect` | Same as bare `/crawdad`. Conversational entry point. |
| `/crawdad checkin` | Wavelength check-in: routes through `creek.state.read` and asks Sonnet to summarise the phase. |
| `/crawdad surface` | Surface paradoxes, liminal content, or emerging themes via `creek.lint`. |
| `/crawdad draft <topic>` | Mine + draft an essay on the supplied topic (routes through `creek.mine` and `creek.draft`). |
| `/crawdad save <content>` | File the supplied content back to the vault via `creek.save`. |
| `/crawdad workflow [list]` | Stub. Returns the v1.1 placeholder; full workflow DSL ships with the next FEAT. |

### Why Discord gets a smaller surface

The Discord conversation is the user's frontend. Six commands is
enough to cover the common moves (check-in, surface, draft, save,
reflect, workflow-list). Deeper inspection happens via the developer's
`/creek` surface in Claude Code.

### Default reply formatting

Discord doesn't render markdown tables well. `/crawdad` replies use
code blocks for structured data (paths, IDs) and bullet lists for
prose summaries. Tables are avoided.

## Privacy tier

Both surfaces inherit the privacy ceiling rules from FEAT-010/011/012.
The Discord bot's MCP client does not get the elevated purge token
(FEAT-013 §31), so `/crawdad` cannot invoke purge tools. The `/creek`
surface in the developer's Claude Code can, if the developer sets the
elevated token in `mcp.json`.

## See also

- `creek-tools/docs/mcp.md` — MCP tool surface and privacy tier rules.
- `crawdad/CLAUDE.md` — CrawDad architecture and quality bar.
- `FEAT-016` — the original slash-command-grammar spec; the `plans/git-issues/` directory was retired in #243, so use `git log --grep='FEAT-016'` for the implementing commits.
