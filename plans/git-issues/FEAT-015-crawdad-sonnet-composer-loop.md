# FEAT-015: CrawDad — Sonnet composer + 5-round loop + voice-skill activation

**Severity:** High (v1.0)
**Category:** FEAT
**Estimated LOC:** ~450
**Estimated complexity:** M
**Source candidate:** [`plans/2026-05-05_comparative-analysis/candidates/ADOPT-008-haiku-router-sonnet-composer.md`](../2026-05-05_comparative-analysis/candidates/ADOPT-008-haiku-router-sonnet-composer.md) (part 3 of 3)
**Dependencies:** FEAT-014
**Parallelizable with peers:** no
**Wave:** 5 (closes CrawDad v1.0)

## Goal

Replace FEAT-014's "boring structured reply" with the Sonnet composer step. Wrap the whole thing in the 5-round loop. Activate the voice-skill tree (`<vault>/creek-skills/`) for prose composition so CrawDad's replies sound like Geoff.

## Files to touch

- `crawdad/crawdad/composer.py` (new) — Sonnet call that takes the user message, tool results, history, and wavelength snapshot, and returns a voice-faithful reply.
- `crawdad/crawdad/skill_loader.py` (new) — loads `<vault>/creek-skills/voice-core/SKILL.md` plus phase- and register-specific skills based on the session state.
- `crawdad/crawdad/loop.py` (new) — orchestrates router → dispatch → (decide-to-loop-or-compose) → composer, capped at 5 rounds.
- `crawdad/crawdad/bot.py` — replace the structured-reply path with `loop.run(message)`.
- `crawdad/tests/test_composer.py`, `tests/test_skill_loader.py`, `tests/test_loop.py` (new).

## Pre-decided choices

- **Composer model:** read from `crawdad/crawdad/config.py:DEFAULT_COMPOSER_MODEL` (with `CRAWDAD_COMPOSER_MODEL` env override), same indirection pattern as FEAT-014's router model. Today's default is the Sonnet model documented in `creek-tools/docs/classification.md` (`claude-sonnet-4-6` at the time of writing); the constant is the contract, not the literal ID. Same no-literal-IDs-outside-config test applies.
- **5-round cap:** hard. After 5 rounds without a final composer call, CrawDad replies with "I went too deep on this — let's back up. Can you reframe?" and resets the session state.
- **Loop termination signal:** the router returns `{ intents: [], compose: true }` to signal "no more tool calls; compose the reply now." Otherwise the loop continues.
- **Voice-skill activation per session:** at session start, load `voice-core/SKILL.md` always; load the phase skill matching the current wavelength snapshot; load the `confessional` register by default (the LTM reflective register). The router can request additional registers via an `activate_register` intent.
- **Voice-fidelity-vs-loop split:** the composer in this loop handles *Discord conversation*. `creek.draft` (called as a tool) is what generates *essays* — the draft has its own LLM call inside `creek-tools` with its own skill-stack assembly. This FEAT does *not* re-implement draft composition; it wraps `creek.draft`'s output in conversational framing.
- **Behavioural commitments (per INTEGRATION-PLAN.md "Conversational chat" section):**
  - Phase-aware: composer prompt includes the current wavelength phase + dosage. Don't urge high-energy action during Bottoming Out.
  - Paradox-tolerant: when tool results include a paradox surfacing, the composer names the paradox and routes to `10-Liminal/Paradoxes/` via `creek.save` (a tool call inside the loop). It never proposes resolution.
  - Voice-faithful: composer always conditions on `voice-core/SKILL.md` plus the loaded register.

## Test plan

- Unit: `composer.compose(...)` against a fixture (recorded Sonnet response) returns the documented structured response.
- Unit: `skill_loader.load_for_session(state)` returns the right skill files for a Bottoming Out session vs. a Rising session.
- Unit: `loop.run(message)` against fixtures with 1, 3, and 5 rounds terminates correctly each time.
- Regression: a 6th round attempt is refused with the documented user message.
- Regression: when tool results include a paradox, the composer's reply names it and triggers a `creek.save --target paradox` tool call within the same loop.
- Regression: composer never calls `creek.draft` directly — `creek.draft` is always invoked as an MCP tool, never inlined into composer prompts. Voice fidelity for drafts is owned by `creek-tools`, not by CrawDad.
- End-to-end: a real-ish Discord conversation (using a record/replay LLM fixture) produces a voice-faithful reply that activates the expected skill stack.

## Acceptance criteria

- Sonnet composer is wired into the 5-round loop.
- Voice-skill loader activates the right skills for the session.
- 5-round cap is hard-enforced.
- Phase-aware, paradox-tolerant, voice-faithful behaviours are verified by regression tests.
- `creek.draft` is never inlined into composer prompts (verified by test that grep-checks composer prompt construction).
- ≥90% branch coverage.
- A documented record/replay test fixture exists for end-to-end behaviour.

## References

- Source candidate: ADOPT-008.
- INTEGRATION-PLAN.md "Voice fidelity through the stack" — this FEAT is the conversational gate.
- FEAT-014 (the router this completes).
- FEAT-016 (slash commands wrap this loop with explicit-trigger entry points).
- Voice-skill tree: produced by `creek skills` (in creek-tools) and read here.
