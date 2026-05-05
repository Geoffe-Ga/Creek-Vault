# REJECT-002: n8n as CrawDad's Agent Substrate

**Verdict:** REJECT
**Source system:** AlfredOS (the productized bundle, not `alfred-vault`)
**Affects:** CrawDad agent layer
**Roadmap target:** N/A
**Estimated complexity:** N/A
**Conflicts with non-negotiables?** none directly — this is a pure technical fit issue

## What it is

The AlfredOS Gumroad bundle uses n8n as the agent substrate: chat triggers (Telegram, WhatsApp) hit n8n webhooks, flows orchestrate LLM calls and tool invocations, bundled apps are exposed as REST targets. The marketing implies autonomous agent behavior; the substrate is a triggered-workflow engine.

## Why it's interesting

It's worth recording why CrawDad shouldn't follow AlfredOS's substrate choice, because the "self-hosted agent stack" framing is seductive and n8n's flow visualization makes prototyping easy.

## Fit with Creek Vault and/or CrawDad

It doesn't fit. n8n is a triggered-workflow engine — it excels at "when X happens, do Y, then Z, then W" with branching and webhooks. It is not optimized for:

- **Long-horizon planning and replanning.** CrawDad's workflow-driven commands ("draft my next Substack on phase transitions") are multi-step pipelines whose branching depends on intermediate LLM outputs. n8n flows can do this but become unreadable; Python or TypeScript with explicit state is cleaner.
- **Conversational state management.** n8n flows are stateless between invocations; CrawDad needs episodic memory (ADAPT-008) and session continuity. Bolting state onto n8n means writing to external storage from inside flows, which negates n8n's visual-flow advantage.
- **Voice fidelity.** Creek's voice-skill tree assumes the drafting LLM has explicit access to skill-stack files. n8n could call LLMs but the skill-stack-management code is in `creek-tools`; routing through n8n adds a layer of indirection without value.
- **MCP integration.** ADAPT-004 commits to creek-tools as an MCP server; CrawDad consumes MCP. n8n's MCP support is via the same plugin everyone else uses — there's no reason to insert n8n between the Discord bot and the MCP server.

The right substrate for CrawDad is a Discord-bot framework (e.g., `discord.py` or `discord.js`) plus a small Python or TypeScript agent runtime that implements the Haiku-router/Sonnet-composer loop directly, calls MCP tools, and writes episodic memory. About 1500 lines of code rather than n8n's flow-visualization plus webhook-glue plus state-bolt-on.

## Reasoning if rejected or deferred

This verdict could flip only if:

- CrawDad's interaction surface expanded to include many platforms (WhatsApp, Slack, iMessage, Telegram), at which point a multi-trigger orchestrator like n8n would have value. The user has explicitly named Discord-first; multi-platform is out of scope.
- The user wanted to expose CrawDad workflows for non-developers to author. The user is the developer; not relevant for personal use.

## Dependencies

- Adjacent to: ADAPT-004 (MCP server is the integration point — CrawDad consumes it directly, no n8n in between).

## Acceptance criteria

N/A — this is a rejection. Documented so the question of "should we use AlfredOS's flow substrate?" doesn't get re-litigated.
