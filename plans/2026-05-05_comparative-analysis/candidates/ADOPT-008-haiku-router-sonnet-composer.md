# ADOPT-008: Haiku-Router + Sonnet-Composer for CrawDad

**Verdict:** ADOPT
**Source system:** Dontoh Alfred
**Affects:** CrawDad agent layer
**Roadmap target:** v1 (CrawDad's foundational agent loop)
**Estimated complexity:** M
**Conflicts with non-negotiables?** none

## What it is

Dontoh's agent loop ([dev.to, Mar 16 2026](https://dev.to/joojodontoh/an-autonomous-agentic-ai-assistant-meet-alfred-and-this-is-how-i-built-him-4e7m)):

```
user message
  ▼
Haiku  ← intent extraction (JSON: { intents: [ { type, query, source, ... } ] })
  ▼
tool execution (MCP tools or native)
  ▼  (loop, max 5 rounds)
Sonnet ← response composition (final user-facing prose)
```

Cost shape: a 3-round conversation costs 3 Haiku calls + 1 Sonnet call rather than 3 Opus calls. The router/synthesizer split is a cost-tuning lever and a clarity-of-architecture choice at once.

Truncation discipline: history truncated to last 20 entries × 2000 chars each.

## Why it's interesting

CrawDad on Discord answers a wide range of message types. Most don't need top-tier synthesis — they need fast intent classification ("you want a wavelength check-in" vs. "you want a draft" vs. "you want me to surface what's in your liminal folder this week") and a short, voice-faithful response. A subset — the actual drafts and the longer reflections — need the more expensive model for prose quality.

Without the split, CrawDad either pays Sonnet/Opus prices for trivial intent extractions, or pays Haiku prices for prose composition that needs more. The split means the right model runs at the right step.

The voice-fidelity tradeoff is worth being explicit about: **Haiku does intent, never composition.** Voice generation always uses Sonnet (or better), and always activates the relevant skill stack from the voice-skill tree. Haiku's job is just to figure out which skills to load.

## Fit with Creek Vault and/or CrawDad

The integration with creek-tools-via-MCP (ADAPT-004):

1. User: "what's surfacing this week?"
2. Haiku: emits `{ intents: [{ type: "vault_state_read" }] }`.
3. Dispatcher calls the MCP tool that maps to `creek state` (or reads the latest `00-Creek-Meta/State/latest.md`).
4. Sonnet: composes a response in CrawDad's voice register, conditioned on the vault state and the user's current wavelength phase.

For drafting:

1. User: "draft my next Substack on phase transitions."
2. Haiku: emits `{ intents: [{ type: "mine", strategy: "thread-terminus" }, { type: "draft", phase: "withdrawal", register: "confessional" }] }`.
3. Dispatcher invokes `creek mine` then `creek draft`.
4. Sonnet: NOT actually used for the draft itself — `creek draft` already calls an LLM with the skill stack. Sonnet only composes the *response wrapper* ("here's the draft I just put in `07-Voice/Drafts/`, the strategy was thread-terminus and the contributing fragments were ..."). The draft itself goes through `creek draft`'s own LLM call.

This is a useful clarification: the Sonnet-composer in the loop handles *Discord conversation*, not the *draft generation* itself. Voice fidelity in drafts comes from `creek draft`'s skill stack, not from this composer. Keeping these separate prevents the loop from accidentally regenerating drafts every time the user asks a clarifying question.

## Translation if adapted

Three Creek-flavored choices:

1. **Wavelength-aware intent extraction.** The Haiku router prompt should know about wavelength phases and suggest phase-appropriate intents. ("If the user's recent fragments are Bottoming Out, prefer `compost-surface` and `paradox-hold` intents over `mine` and `draft`.")
2. **Privacy tier in intent payload.** Every intent that touches the vault carries a privacy_tier field; the dispatcher refuses intimate-tier reads unless explicitly authorized.
3. **`intents` schema lives next to the MCP server schema.** The intent types should map 1:1 to MCP tool names, so the router can be regenerated from the tool registry rather than maintained separately.

Defaults to start from: max 5 rounds (as Dontoh has it), history truncation at 2000 chars/entry × 20 entries (same), Haiku for intent extraction (`claude-haiku-4-5-20251001` as configured in creek-tools today), Sonnet 4.6 for composition (`claude-sonnet-4-6`). Tune later.

## Dependencies

- Pairs with: ADAPT-004 (MCP server — intents map to MCP tools).
- Reads: ADOPT-005 / ADOPT-007 (audit report / index — Haiku reads the index summary; Sonnet conditions on it).

## Acceptance criteria

- CrawDad's agent loop is implemented with Haiku for intent extraction and Sonnet for composition.
- The `intents` JSON schema is documented and matches the MCP tool registry one-to-one.
- The 5-round loop cap is hard-enforced; no recursion past the cap.
- History truncation at 2000 chars/entry × 20 entries is the default.
- `creek draft` is *not* called from the Sonnet composer — it's called as a tool by the dispatcher, with its own LLM call inside.
- Voice fidelity for drafts is verified by an end-to-end test: a known fragment seeds a draft, the draft activates the expected skill stack, and the output is sampled against a reference voice metric (e.g., a stylometry check or a reviewer skill).
- Cost per typical 3-round conversation is documented and reproducible.
