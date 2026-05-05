# ADAPT-007: Slash-Command Grammar for CrawDad and Claude Code

**Verdict:** ADAPT
**Source system:** Graphify (`/graphify` family)
**Affects:** CrawDad agent layer + developer's Claude Code experience
**Roadmap target:** v1 (`/creek` family for Claude Code), v0.2 (`/crawdad` family for Discord)
**Estimated complexity:** S
**Conflicts with non-negotiables?** none

## What it is

Graphify ships a coherent slash-command grammar usable from any agent client:

- `/graphify .` — build initial graph
- `/graphify ./folder --update` — incremental
- `/graphify --watch` — auto-sync
- `/graphify query "..."` — graph traversal answer
- `/graphify path A B` — shortest path
- `/graphify explain X` — plain-English summary

Cited from [README v3](https://github.com/safishamsi/graphify/blob/v3/README.md) and the v4/v5 evolutions. Distribution as a skill across multiple agent platforms (Claude Code, Codex, Cursor, etc.) is more thoughtful than most knowledge tools.

## Why it's interesting

Creek has 13 CLI commands but no consistent slash-command surface for either Claude Code or CrawDad. The user has named **standard skill commands** as one of three CrawDad interaction modes — natural language equivalents like "lint the vault," "what's surfacing this week," "draft my next Substack" should reduce to a small, named command set.

Graphify's grammar has good ergonomic instincts: a default action when invoked without args, common subcommands (`query`, `explain`, `path`), incremental flags (`--update`, `--watch`), and a help discoverable from any prefix. Borrowing the grammar gives CrawDad and Claude Code consistency.

## Fit with Creek Vault and/or CrawDad

Proposed `/creek` commands for Claude Code (one per MCP tool, mostly):

```
/creek                       → /creek state (default action)
/creek state                 → render audit report
/creek lint [--check NAME]   → run hygiene
/creek mine [--strategy X]   → mine essay seeds
/creek draft [--index N]     → draft from top idea
/creek save <type>           → file-back current conversation
/creek explain <fragment>    → plain-English summary of a fragment/eddy/thread
/creek phase                 → current wavelength snapshot
/creek wavelength <period>   → wavelength report
/creek skills refresh        → regenerate voice skill tree
/creek ingest --type X       → run an ingestor
/creek purge ...             → destructive ops (gated)
```

Proposed `/crawdad` commands for Discord (a small subset — most CrawDad interaction is conversational, not command):

```
/crawdad reflect             → reflective conversation mode
/crawdad checkin             → wavelength check-in
/crawdad surface             → surface what's interesting from the vault right now
/crawdad draft <topic>       → request a draft
/crawdad save <type>         → file-back this conversation
/crawdad workflow run <id>   → run a Jig-style composite workflow (ADAPT-003)
```

The CrawDad commands are small because conversational mode (ADOPT-008's Haiku-router) handles natural language; explicit slash commands are for when the user wants the bot to perform a specific operation deterministically.

## Translation if adapted

Three considerations:

1. **Don't proliferate commands.** Graphify has ~10; Creek has 13. CrawDad should have ~6. Each command needs to earn its place; ambiguous-but-related operations should be parameters on a single command, not new commands.
2. **The default action matters.** `/creek` (no args) should do the most useful thing — render the audit report. `/crawdad` should open reflective conversation mode.
3. **Help is discoverable from any prefix.** `/creek help`, `/creek mine help`, `/crawdad help` all return useful, scoped help. This is mostly Typer/CLI convention but matters for slash-command UX.

## Dependencies

- Depends on: ADAPT-004 (MCP server — slash commands invoke MCP tools).
- Pairs with: ADOPT-008 (Haiku router can be invoked by either explicit slash or conversational message).

## Acceptance criteria

- A `/creek` slash-command surface is registered with Claude Code via the standard skill mechanism.
- A `/crawdad` slash-command surface is implemented in the Discord bot.
- Both surfaces invoke MCP tools, not subprocess calls to the `creek` CLI.
- The slash commands are documented in `creek-tools/README.md` and CrawDad's README.
- Each command has scoped help via `<command> help`.
- Discord-specific help renders with Discord-friendly formatting (code blocks, no markdown links the client won't render).
