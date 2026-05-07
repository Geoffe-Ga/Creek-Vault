# FEAT-016: `/creek` (Claude Code) and `/crawdad` (Discord) slash-command grammars

**Severity:** High (v1.0)
**Category:** FEAT
**Estimated LOC:** ~400
**Estimated complexity:** S
**Source candidate:** [`plans/2026-05-05_comparative-analysis/candidates/ADAPT-007-slash-command-grammar.md`](../2026-05-05_comparative-analysis/candidates/ADAPT-007-slash-command-grammar.md)
**Dependencies:** FEAT-010/011/012 (MCP server), FEAT-015 (CrawDad loop)
**Parallelizable with peers:** no (closes the v1.0 work)
**Wave:** 5

## Goal

Two small surfaces with a consistent grammar, both invoking the same MCP tools so Discord and Claude Code feel like one system from two front doors.

## Files to touch

- `creek-tools/.claude/commands/` (new directory) — one `*.md` slash command per `/creek <subcommand>`. These are Claude Code skill commands.
- `creek-tools/.claude/commands/creek.md` (new) — root help; default action is render `creek state`.
- `creek-tools/.claude/commands/{state,lint,mine,draft,save,explain,phase,wavelength,skills,ingest}.md` (new) — one per command.
- `creek-tools/docs/slash-commands.md` (new) — documents the `/creek` family.
- `crawdad/crawdad/slash_commands.py` (new) — Discord slash-command registration for the six `/crawdad` commands.
- `crawdad/crawdad/bot.py` — register slash commands at startup; route to existing handlers.
- `crawdad/tests/test_slash_commands.py` (new).
- `crawdad/README.md` — document the six commands.

## Pre-decided choices

- **Slash-command surface (small on purpose):**
  - `/creek state | lint | mine | draft | save | explain | phase | wavelength | skills | ingest` (~10 for the developer's Claude Code).
  - `/crawdad reflect | checkin | surface | draft | save | workflow` (6 for Discord).
- **Default action:**
  - `/creek` (no args) → `creek state`.
  - `/crawdad` (no args) → opens reflective conversation mode (FEAT-015's loop with no preselected intents).
- **All slash commands invoke MCP tools, not subprocess CLI calls.** This keeps Discord, Claude Code, and any future client on one tool surface.
- **`/creek` commands live as Claude Code skill files** under `creek-tools/.claude/commands/` — these are *not* part of the `creek/` Python package. Each file is a markdown skill (frontmatter + body) that Claude Code reads when the user types `/creek <subcommand>`. This convention is added to the project; if `creek-tools/CLAUDE.md` doesn't already document `.claude/commands/`, this FEAT also adds a one-paragraph note there pointing at the new directory.
- **Help discoverable from any prefix:** `/creek help`, `/creek mine help`, `/crawdad help` all return scoped help.
- **Discord-specific formatting:** Discord doesn't render markdown link footnotes well, so `/crawdad` commands return code blocks for structured data and bullet lists for prose summaries. No tables.
- **`/crawdad workflow` is a stub in v1.0** — it advertises the workflow DSL coming in v1.1 (ADAPT-003) but only supports `/crawdad workflow list` (returns "no workflows yet — coming in v1.1") in this PR. Full implementation lands with the v1.1 workflow DSL FEAT.

## Test plan

- Unit: each `/creek <cmd>` command file is valid Claude Code skill markdown (frontmatter parses).
- Unit: each `/crawdad <cmd>` registration succeeds against a mocked Discord client.
- Integration: invoking `/creek state` from a Claude Code session against a fixture vault returns the rendered audit report.
- Integration: invoking `/crawdad surface` from a Discord client (mocked) triggers the same MCP `creek.state.read` tool that the conversational mode would.
- Regression: `/crawdad <unknown>` returns a help message naming the six valid commands, not a stack trace.

## Acceptance criteria

- 10 `/creek` slash commands exist as Claude Code skill files in `creek-tools/.claude/commands/`.
- 6 `/crawdad` slash commands registered with Discord and routed through MCP.
- Default actions documented and working (no-arg invocation does something useful).
- Help discoverable at every prefix level.
- `/crawdad workflow` stub returns the v1.1 placeholder cleanly.
- ≥90% branch coverage on `crawdad/crawdad/slash_commands.py`.
- `creek-tools/docs/slash-commands.md` and `crawdad/README.md` document the surfaces.

## References

- Source candidate: ADAPT-007.
- Graphify's `/graphify` family is the prior art for grammar consistency.
- FEAT-010/011/012 (the MCP tools these commands invoke).
- FEAT-015 (the CrawDad loop these commands enter).
