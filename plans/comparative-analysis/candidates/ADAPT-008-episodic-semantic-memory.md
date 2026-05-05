# ADAPT-008: Episodic / Semantic Memory Split for CrawDad's Conversation State

**Verdict:** ADAPT
**Source system:** Dontoh Alfred (April 2026 follow-up)
**Affects:** CrawDad agent layer
**Roadmap target:** v0.2
**Estimated complexity:** M
**Conflicts with non-negotiables?** none, but trim the cognitive-science framing

## What it is

Dontoh's [memory system follow-up](https://dev.to/joojodontoh/teaching-alfred-to-remember-with-a-neuroscience-inspired-memory-system-for-ai-agents-2o5l) splits agent memory into **episodic** (specific events/conversations) and **semantic** (consolidated facts and patterns), with an Ebbinghaus-style temporal decay function and "dreaming" consolidation cycles. The neuroscience framing is mostly aesthetic — recency bias falls out of vector-search-with-time-weighting whether you call it Ebbinghaus or not — but the *split* is useful.

## Why it's interesting

CrawDad's conversation state has two components that should be stored differently:

- **Episodic:** a record of recent Discord conversations — who said what, when, what tools were called, what was filed back. Useful for the next 5–7 days of conversation continuity. Weight decays naturally.
- **Semantic:** consolidated patterns — "the user prefers reflective questions to declarative reframes," "the user is in week 3 of a Withdrawal phase," "the user filed back three Substack drafts last month and rejected one." Useful indefinitely. Should not decay.

Without the split, a single conversation log either bloats indefinitely or loses long-term patterns. Splitting them lets each be optimized.

The semantic layer in this design overlaps with — but is not the same as — Creek's compiled layer. Compiled-layer pages are about the *user's content*; semantic memory is about the *user's interaction patterns with CrawDad*. Both useful, both belong somewhere, but they're different concerns.

## Fit with Creek Vault and/or CrawDad

CrawDad-only. Two storage targets:

1. **Episodic memory:** ephemeral or short-lived storage (SQLite, JSONL, or a Discord-bot-conversation table). Records: `{user_id, timestamp, message, intents_emitted, tools_called, response_excerpt}`. Pruned after N days (configurable; default 14).
2. **Semantic memory:** lives in the vault, in `08-Decisions/Crawdad-Patterns.md` or a sibling location. Updated by a periodic consolidation run that reads episodic memory and surfaces patterns. Patterns are filed as Praxis-flavored notes with Geoff's review.

The consolidation cadence is a worker — Surveyor (in the four-worker decomposition from ADAPT-002) is a reasonable home, since the pattern-discovery work is surveying conversation patterns rather than fragment relationships. Or it's a fifth worker; that's fine too.

## Translation if adapted

Three Creek-specific adaptations:

1. **Drop the neuroscience framing.** "Dreaming consolidation cycles" is aesthetic. Call it "weekly pattern consolidation." Run it Sunday nights via cron or scheduled MCP call.
2. **Semantic memory writes go through the vault, not into a separate store.** Patterns CrawDad discovers about its conversations with the user become first-class vault content (with a privacy tier — likely `personal` by default). This keeps Creek as the single source of truth for "what the system knows about the user" and avoids a second knowledge silo.
3. **Episodic memory respects privacy tiers.** A conversation about an intimate-tier topic is logged at intimate tier; consolidation runs that surface intimate-tier patterns require explicit consent before writing them to the vault.

The Ebbinghaus decay function and "dreaming" can both be replaced by simpler designs: prune episodic memory by age (cron), run pattern consolidation on a fixed schedule (cron), filter recent episodic entries by recency when the agent loads context (trivial). No decay function needed.

## Dependencies

- Depends on: ADAPT-004 (MCP server — episodic memory is keyed on MCP tool invocations).
- Pairs with: ADOPT-008 (the agent loop writes the episodic record at each turn).

## Acceptance criteria

- An episodic memory store exists with documented schema, pruning policy, and privacy-tier handling.
- A periodic consolidation job reads episodic memory and writes pattern notes to the vault.
- Pattern notes carry privacy tiers; intimate-tier patterns require explicit consent before being written.
- CrawDad's agent loop writes one episodic record per turn (user message + intents + tools called + response).
- The next session's Haiku router reads recent episodic memory + current semantic patterns from the vault.
- A regression test verifies episodic pruning doesn't drop entries flagged as "promote-to-semantic."
