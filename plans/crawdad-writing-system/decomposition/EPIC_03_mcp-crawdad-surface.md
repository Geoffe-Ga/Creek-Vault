## Epic Summary

Surface the Creek Writing Desk beyond the CLI: expose an `author` verb over the creek-tools MCP
surface, and wire CrawDad's slash commands (`/crawdad ask`, `/crawdad draft`) to call it so the
Discord side answers research questions and drafts via the same desk. AI-authored outputs the
owner keeps are saved back to `11-Other-Authors/ai-as-user/` with correct attribution.
Implements SPEC §4.3 (adapters) and §10 (MCP + CrawDad).

## Scope

**In scope:**
- New `author` MCP tool mirroring the `creek author` CLI args, returning draft + provenance + reflection verdict over stdio.
- CrawDad `/crawdad ask <question>` (research medium) and `/crawdad draft <topic>` routed to the MCP `author` verb.
- Saving kept AI output to `11-Other-Authors/ai-as-user/<slug>/` with `representativeness: endorsed`, `voice_weight: 0.0`.
- User + channel allowlist enforcement and graceful degradation when the desk / creek-tools is unreachable.

**Out of scope:**
- The desk internals (EPIC_02).
- Mediums other than research/draft (EPIC_04).

## Success Criteria

The epic is done when:

- [ ] An MCP client calls the `author` tool over stdio and receives a well-typed draft + provenance + verdict.
- [ ] `/crawdad ask` answers a research question about APTITUDE/Wavelength end-to-end in Discord.
- [ ] A kept AI draft is filed to `11-Other-Authors/ai-as-user/` with correct attribution and exits the desk's privacy gates.
- [ ] Non-allowlisted callers get no response; an unreachable desk yields a soft error, not a crash.
- [ ] All child issues closed; both `creek-tools` and `crawdad` quality gates green on `main`.

## Child Issues

_Filled in after child issues are filed._

- [ ] #NNN — Skeleton: MCP `author` verb stub returning the output shape
- [ ] #NNN — Core: wire the MCP `author` verb to the real desk
- [ ] #NNN — Core: CrawDad `/crawdad ask` + `/crawdad draft` routing + ai-as-user save
- [ ] #NNN — Edges: allowlist + graceful degradation

## Sequencing Notes

- **Blocked by:** EPIC_02 (the desk must exist).
- **Parallel-safe:** EPIC_04.

## SPEC Reference

`plans/crawdad-writing-system/SPEC.md` — §4.3 (where it lives / adapters), §4.4 (SDK), §10 (CLI & MCP surface), §7 (`ai-as-user` save target).

## Labels

`epic`, `spec-decomposition`, `mcp`, `crawdad`
