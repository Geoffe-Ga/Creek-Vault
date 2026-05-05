# AlfredOS

## Source URLs

- AlfredOS 1.0 announcement (Substack): <https://lumberjackai.substack.com/p/alfredos-10-is-here>
- 1.0 announcement mirror: <https://lumberjack.so/alfredos-10-is-here/>
- Install in 10 Minutes: <https://lumberjackai.substack.com/p/install-alfred-in-10-minutes>
- "Becoming an AI-First Operator": <https://lumberjack.so/becoming-an-aifirst-operator/>
- Gumroad listing: <https://ssdavidai.gumroad.com/l/hydrfd>
- Screenless Dad persona: <https://screenlessdad.com/>
- `alfred-vault` GitHub: <https://github.com/ssdavidai/alfred>
- Author Substack: <https://substack.com/@ssdavid>

## What it is

Two related projects under the same author (David Szabo-Stuban / "The Screenless Dad"). **AlfredOS** is a paid Gumroad bundle: a Docker-Compose-style preinstalled stack of self-hosted apps (Kutt, NocoDB, Supabase, ~16 apps total) plus an n8n-flavored multi-agent workflow layer, marketed as a "self-hosted business operating system for solopreneurs." **`alfred-vault`** is a more serious open-source Python project (MIT, ~93% Python / 7% TypeScript) where the real engineering happens — a six-layer architecture for a personal agentic infrastructure tending an Obsidian vault. Marketing collapses the two; this brief treats them as distinct.

A separate piece — **Jig** — is a markdown-flavored DSL for declaring business workflows that compiles into MCP servers, sitting on top of AlfredOS as middleware so Claude Desktop / Cursor can drive AlfredOS workflows through MCP tool-calls.

## Architecture

`alfred-vault`'s six layers (the design worth studying):

```
┌─ Interface ─ Telegram, WhatsApp, Slack, iMessage, email, CLI, TUI
├─ Agent ──── pluggable: Claude Code (subprocess), Zo Computer (HTTP), OpenClaw (subprocess)
├─ Kinetic ── Temporal-based durable workflow engine, cron scheduling
├─ Semantic ─ Obsidian vault, 20 record types, YAML frontmatter, [[wikilinks]]
├─ Data ───── ambient capture: Omi transcripts, Zoom recordings, email, RSS
└─ Infra ──── self-hosted: Mac Mini / VPS / "personal cloud"
```

**Four background workers** tend the semantic layer continuously:
- **Curator** — processes inbox files into structured records.
- **Janitor** — fixes broken links, orphaned files.
- **Distiller** — surfaces implicit assumptions and contradictions.
- **Surveyor** — semantic embeddings, relationship discovery.

**Jig** (the DSL):
- "Markdown for operations" — human-readable, MCP-native syntax for workflow steps and required tools.
- Compilation target: an MCP server that Claude Desktop / Cursor / any MCP-aware client can consume.
- Skill-based: workflows decompose into deterministic steps stored in a database; each skill has a narrow scope, specific tools, and clear guidance. The LLM acts as a router-of-skills rather than a free-form executor.
- *Cannot verify exact syntax* (Substack returned 403 to fetch attempts; no public spec found in indexed sources).

The AlfredOS bundle itself is **n8n-as-agent-substrate**: chat triggers (Telegram, WhatsApp) hit n8n webhooks, flows orchestrate LLM calls and tool invocations, bundled apps are exposed as REST targets. No-code DNA, distinct from `alfred-vault`'s Python-first stack.

## Wins

- **Four-worker decomposition (Curator/Janitor/Distiller/Surveyor).** This is the single most copyable design in the project — a clean separation of *what an Obsidian vault assistant should do continuously* into four named, separable cadences. Directly applicable as a CrawDad pattern.
- **MCP-as-capability-surface.** Authoring business logic once and exposing it via MCP — so any MCP-aware client (Claude Desktop, Cursor, Claude Code, Zed) can drive it without learning a new runtime — is strategically correct. This is the same play as Anthropic's Skills.
- **Jig's "deterministic skills decomposition" instinct.** Treating business workflows as authored artifacts (declarative, reviewable, versionable) rather than LLM improvisation reduces hallucination on repeatable processes.
- **Temporal as the durable-workflow engine.** Real engineering choice, not vibes. Long-horizon agent tasks need replay, retry, and durability — ad-hoc Python scripts don't survive contact with reality.
- **Six-layer architecture is legible.** Interface / Agent / Kinetic / Semantic / Data / Infra is a defensible decomposition; each layer can be swapped without redesigning the others.
- **Local-first for apps and data.** Self-hosted Postgres, Supabase, NocoDB — the data sovereignty story for stored content is real.

## Costs

- **n8n-as-agent-substrate is wrong for genuinely autonomous behavior.** n8n is a triggered-workflow engine optimized for branching webhooks, not for long-horizon planning, replanning, or recovery. Marketing implies autonomy n8n alone won't deliver.
- **"Operating system" is marketing.** It's a Docker-Compose with extras. Useful framing, but the OS metaphor obscures what's actually there.
- **"Screenless" is rhetorical.** Chat is still a screen. The phrase means "no SaaS dashboards" / "ambient capture," not "no display."
- **Inference is cloud, not local.** Apps and data stay local; reasoning ships to Anthropic via Claude Code / Claude Desktop / Cursor. The "self-hosted" wedge weakens at the LLM layer; nothing in the marketing leans on local models as default.
- **Conflation of `alfred-vault` (real engineering) with AlfredOS (no-code product bundle)** benefits the marketing but obscures which thing you're actually evaluating.
- **Jig's value depends on unseen syntax.** If it's "YAML with prose descriptions," the LLM is doing 95% of the work and Jig is decoration. If it has real semantics — typed steps, idempotency markers, retry policies, side-effect declarations — it could matter. Cannot verify which without the spec.
- **Single-developer project, paid via Gumroad, ~39 buyers cited at launch.** Bus-factor and sustainability risks are real.
- **Bundled-SaaS-replacement scope is wrong for personal use.** Solopreneurs need Kutt and NocoDB; an LTM building a spiritual companion bot does not.

## Relevance to Creek Vault, CrawDad, or both

**CrawDad, primarily** — this is the prior art for the agent-layer of the project, and a competitor in the loose sense the prompt frames it as.

The directly portable ideas:
1. **Four-worker decomposition** maps onto Creek's existing emergence infrastructure (Tag Garden, Unnamed Digest, Synchronicity, Compost, Paradox) but reframes them as *background workers running on a cadence* rather than as report types invoked by hand. Curator → ingest+classify+link. Janitor → broken-link cleanup, orphan detection. Distiller → paradox + synchronicity surfacing. Surveyor → resonance/eddy discovery. **The four-name grouping is more legible than Creek's current five-report-type fragmentation; renaming is part of the integration.**
2. **MCP-as-capability-surface** is the right answer for how CrawDad reaches creek-tools. Rather than CrawDad shelling out to `creek` CLI commands, expose the toolchain as an MCP server; CrawDad (running on Discord) calls MCP tools; same MCP server is reusable from Claude Desktop / Cursor for the developer's own use without duplicating logic.
3. **Jig-style workflow DSL** maps onto CrawDad's workflow-driven commands ("draft my next Substack on phase transitions," "generate an APTITUDE module exercise for the Withdrawal phase," "give me a Wavelength check-in for the last week"). These are composite operations; declaring them in a Jig-flavored DSL makes them authored artifacts the user can review and version, rather than ad-hoc prompt strings.
4. **Six-layer separation** is a useful frame for CrawDad's own architecture: Interface (Discord) / Agent (Haiku-router + Sonnet-composer) / Kinetic (workflow engine, possibly Temporal-lite) / Semantic (creek-tools-via-MCP) / Data (Discord webhook events, ambient capture is out of scope) / Infra (Hostinger VPS, deferred to separate prompt).

What to **not** borrow:
- **n8n** as the agent substrate. CrawDad is text-first via Discord; the right substrate is a Python or TypeScript Discord-bot framework with direct MCP client capability, not n8n flows.
- **Voice/screenless ambition.** Discord-first means text-first; don't fight that.
- **The bundled-SaaS-replacement scope.** No Kutt, no NocoDB. Pick the integrations you actually use.
- **The "OS" framing.** Just call it CrawDad.

**Creek Vault, secondarily.** The Curator/Janitor/Distiller/Surveyor naming reframe applies to the data layer too — `creek lint` (the Karpathy-derived unified hygiene command) and the existing emergence reports could be reorganized along the four-worker axis. This is more of a vocabulary refactor than an architectural change, but vocabulary refactors that clarify mental models are worth doing once.
