# FEAT-014: CrawDad — Haiku router + tool dispatcher

**Severity:** High (v1.0)
**Category:** FEAT
**Estimated LOC:** ~450
**Estimated complexity:** M
**Source candidate:** [`plans/2026-05-05_comparative-analysis/candidates/ADOPT-008-haiku-router-sonnet-composer.md`](../2026-05-05_comparative-analysis/candidates/ADOPT-008-haiku-router-sonnet-composer.md) (part 2 of 3)
**Dependencies:** FEAT-013
**Parallelizable with peers:** no (FEAT-015 extends this)
**Wave:** 5

## Goal

Add the Haiku-driven intent extraction step to CrawDad's agent loop. Given a user message + recent conversation history + the loaded session state, Haiku emits a JSON `intents` array; the dispatcher invokes the matching MCP tools.

## Files to touch

- `crawdad/crawdad/router.py` (new) — Haiku call + JSON parsing + schema validation.
- `crawdad/crawdad/dispatcher.py` (new) — maps `intent.type` → MCP tool call.
- `crawdad/crawdad/intents.py` (new) — Pydantic schema generated from the MCP server's tool registry (so router prompt + dispatcher stay in sync with FEAT-010/011/012).
- `crawdad/crawdad/history.py` (new) — episodic-memory-lite: in-memory deque of last 20 conversation entries, each truncated to 2000 chars.
- `crawdad/crawdad/bot.py` — wire the router + dispatcher into the message handler (no Sonnet composer yet; FEAT-015 closes the loop).
- `crawdad/tests/test_router.py`, `tests/test_dispatcher.py`, `tests/test_intents.py`, `tests/test_history.py` (new).

## Pre-decided choices

- **Router model:** read from `crawdad/crawdad/config.py:DEFAULT_ROUTER_MODEL`, which itself reads from `CRAWDAD_ROUTER_MODEL` env var with a fallback to whatever Haiku model `creek-tools/docs/classification.md` documents as current at implementation time. Do *not* hard-code the literal model ID string in agent code — model IDs move (today: `claude-haiku-4-5-20251001`; the constant is the contract, not the literal). Verified by a test that asserts no module under `crawdad/crawdad/` references a model ID string outside `config.py`.
- **History truncation:** last 20 entries, each truncated to 2000 chars. Hard cliff per ADOPT-008; FEAT-016 refines this with smarter compression if needed.
- **Intents schema:** generated from the MCP server's `tools/list` response so the router prompt always reflects the current tool surface. Regenerated at CrawDad startup; cached for the session.
- **Router prompt structure:**
  - Role: "Intent-extraction router for the CrawDad spiritual companion."
  - Context: current wavelength snapshot (from session state), last 20 truncated history entries, the available `intents` schema.
  - Output: strict JSON `{ intents: [ { type, ...args } ] }` with no prose.
- **Wavelength-aware intent biasing:** the router prompt explicitly instructs Haiku to prefer phase-appropriate intents (don't suggest `mine`/`draft` during Bottoming Out; prefer `surface_paradox`/`compost`).
- **Per-intent privacy_tier:** every intent payload carries a `privacy_tier_ceiling` field; defaults to the user's session-default tier (currently `open` for the developer; configurable per-channel in v1.1).
- **No Sonnet call in this PR.** The dispatcher returns tool results to the bot handler, which posts them to Discord as a structured (boring) summary. FEAT-015 swaps that summary for Sonnet composition.

## Test plan

- Unit: `router.extract_intents(message, history, state)` against a fixture message returns a structured `IntentsArray`.
- Unit: `dispatcher.dispatch(intent)` calls the right MCP tool with the right args.
- Unit: `history.append(...)` enforces the 20-entry / 2000-char-per-entry truncation.
- Integration: a Discord DM "what's surfacing this week?" produces a `{ type: "creek.state.read" }` intent and the dispatcher returns the latest state content.
- Regression: a malformed Haiku response (non-JSON, missing `intents` key) is caught and surfaces a structured error to the user, not a crash.
- Regression: an intent with an unknown `type` is rejected by the dispatcher with a clear error.
- Regression: phase-aware bias — given a Bottoming Out wavelength snapshot, Haiku does *not* emit `creek.draft` intents (verified with a recorded LLM response fixture, since live LLM tests are flaky).

## Acceptance criteria

- `crawdad run` handles a Discord message via Haiku → dispatcher → tool call → boring structured reply.
- `intents` schema is generated from the MCP tool registry at startup.
- History truncation is enforced (verified by test).
- Wavelength-aware bias is documented in the router prompt and verified by a fixture-based regression test.
- ≥90% branch coverage.

## References

- Source candidate: ADOPT-008 (especially the `intents`-array JSON schema and the cost-shape rationale).
- Dontoh's Alfred (`plans/2026-05-05_comparative-analysis/systems/alfred-dontoh.md`) for the original two-LLM split.
- FEAT-010/011/012 (the MCP server whose tool registry becomes the intents schema).
