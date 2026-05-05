# DEFER-001: Multi-Agent Client Mesh

**Verdict:** DEFER
**Source system:** Karpathy LLM Wiki community implementations ([Ar9av/obsidian-wiki](https://github.com/Ar9av/obsidian-wiki))
**Affects:** Both, theoretically; neither, practically
**Roadmap target:** unscheduled
**Estimated complexity:** L
**Conflicts with non-negotiables?** none

## What it is

Several community implementations of Karpathy's pattern (notably [Ar9av/obsidian-wiki](https://github.com/Ar9av/obsidian-wiki) with its `setup.sh` symlinking) support multi-agent client mesh: the same wiki is consumed by Claude Code, Cursor, Windsurf, Codex, Gemini CLI, and GitHub Copilot via shared `CLAUDE.md`/`AGENTS.md` symlinks plus a `.manifest.json` for delta tracking. The premise: if you compile a wiki once, all your coding agents should benefit from it.

## Why it's interesting

The premise is correct in a shallow sense (a wiki is plain markdown; any agent that can read markdown benefits). The infrastructure to make it cross-agent — symlink farms, manifest tracking, multi-agent merge-conflict handling on parallel writes, per-agent quirks in skill-format expectations — is non-trivial and only pays off if the user actually drives many agents from one wiki.

## Fit with Creek Vault and/or CrawDad

The user has explicitly named:
- **Discord-first** as CrawDad's interaction surface.
- **CLI for the developer's own use.**
- **Personal use only.**

There are exactly two consumers in scope: CrawDad (via MCP) and the developer's Claude Code (via MCP and slash commands). Both consume creek-tools-via-MCP (ADAPT-004). The MCP server is the integration point; multi-agent mesh adds zero value because there's no third agent.

If the user later decided to drive Cursor or Codex against the same vault, the MCP server already supports this — MCP is multi-client by design. No symlink farm needed.

## Reasoning if rejected or deferred

The verdict is DEFER rather than REJECT because:
- The premise isn't wrong; it just isn't valuable for the current scope.
- The MCP server already provides most of what a multi-agent mesh would provide, without the complexity.
- If the user's interaction surface ever expanded (e.g., dedicated VS Code Claude Code workflows + Cursor for a different work mode + Discord for CrawDad), the question "should we add cross-agent symlink discipline?" might become live. But it isn't now.

The verdict could flip if:
- The user adopted a third interaction surface beyond Discord + Claude Code.
- Cross-platform skill-format quirks emerged that the MCP boundary couldn't paper over.

## Dependencies

- Subsumed by: ADAPT-004 (MCP server is the multi-client integration point).

## Acceptance criteria

N/A — deferred. If revisited, the candidate should be rewritten as ADOPT or ADAPT against the then-current consumer set.
