# Dontoh's Alfred (sidebar)

## Source URLs

- "Meet Alfred" (dev.to, Mar 16 2026): <https://dev.to/joojodontoh/an-autonomous-agentic-ai-assistant-meet-alfred-and-this-is-how-i-built-him-4e7m>
- "Teaching Alfred to Remember" (dev.to, Apr 19 2026): <https://dev.to/joojodontoh/teaching-alfred-to-remember-with-a-neuroscience-inspired-memory-system-for-ai-agents-2o5l>
- Author profile: <https://dev.to/joojodontoh>

## What it is

A personal agentic assistant by Joojo Dontoh. Unrelated to AlfredOS (coincidence of name). Lives between inbox, calendar, DevOps boards, Teams, and even a robot vacuum. Architectural headline: **a two-LLM split** — Haiku for tool-routing, Sonnet for response composition — inside a fixed-loop agent runtime. A follow-up post adds a neuroscience-inspired memory system.

## Architecture

The two-LLM agent loop:

```
user message
  │
  ▼
[ IntentExtractionStrategy: Claude Haiku ]
  • input: user message + last 20 history entries (truncated 2000 chars each) + prior round results
  • output: { intents: [ { type: tool_name, query, source, timeMin, ... } ] }
  │
  ▼
[ Tool execution: each intent dispatched to a registered tool ]
  │
  ▼
[ Loop: up to 5 rounds ]
  │
  ▼
[ Response composition: Claude Sonnet ]
  • input: original message, all tool results, history
  • output: user-facing response
```

**Cost shape:** a 3-round conversation costs 3 Haiku calls plus 1 Sonnet call rather than 3 Opus calls. The router/synthesizer split is a cost-tuning lever, not just an architectural choice.

**Memory system** (April follow-up):
- Episodic vs. semantic split.
- Ebbinghaus-style temporal decay function ("a memory from yesterday outranks a memory from last month").
- "Dreaming" consolidation cycles — periodic batch reindexing dressed in cognitive-science vocabulary.
- Credits Peter Steinberger's OpenClaw memory work for converging on similar primitives.

**Integrations:** Google Workspace, Microsoft 365 (Teams), DevOps boards (likely Azure DevOps), email, calendar, robot vacuum.

## Wins

- **Router/synthesizer split with cheap-model-for-mechanical-decisions, expensive-model-for-language.** Well-known pattern, but Dontoh's articulation — JSON `intents` array, fixed 5-round cap, history truncation at 2000 chars per entry — is a clean, copyable specification.
- **`intents`-array JSON schema** is a tidy tool-selection contract. Every tool registered in the runtime has a `type` matching an intent type; the router emits typed intents; the dispatcher looks up by type.
- **Fixed loop cap** (5 rounds) prevents runaway cost. Hard ceiling beats hopeful prompting.
- **Episodic/semantic memory split** is useful even without the neuroscience varnish.

## Costs

- **The neuroscience framing is mostly aesthetic.** Recency bias falls out of vector-search-with-time-weighting whether you call it Ebbinghaus or not. "Dreaming" consolidation cycles are batch reindexing.
- **History truncation at 2000 chars/entry** is a hard cliff that loses long messages mid-context. Real implementations need smarter compression for the tail, but truncation is fine for v1.
- **Personal blog post, not a product.** "Deep contextual understanding of voice and writing style" on the roadmap is the marketing-iest sentence; expect it to mean style-transfer prompts, not anything model-level.
- **Microsoft-365 / robot vacuum integrations** are orthogonal to anything Creek/CrawDad cares about.

## Relevance to Creek Vault, CrawDad, or both

**CrawDad only.** Two ideas worth porting:

1. **The Haiku-router / Sonnet-composer split** is the right pattern for CrawDad's conversational mode on Discord. Most messages don't need Sonnet-quality synthesis; they need fast intent extraction (which creek-tools command does this map to?) and a short response. The 5-round cap and 2000-char history truncation are reasonable defaults to start from.
2. **The `intents`-array JSON schema** as a tool-selection contract maps cleanly onto exposing creek-tools via MCP — each MCP tool corresponds to one `intent.type`. Same dispatcher pattern.

What to **not** borrow:
- The neuroscience-inspired memory framing. Run consolidation on a cron, call it batch reindexing, move on. The episodic/semantic split is fine; the Ebbinghaus dressing isn't load-bearing.
- The robot vacuum / Microsoft 365 integrations. Not relevant.
- The "deep contextual understanding of voice and writing style" roadmap line. Creek's voice-skill tree is more architecturally serious than anything Dontoh has shipped here; don't import vague aspirations.

**Creek Vault: not relevant.** This is purely an agent-layer reference.
