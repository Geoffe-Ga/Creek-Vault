FEAT-040.8: Idiolect-driven prompt prevention — steer toward the user's measured voice

## Context
The primary lever. Prevention beats repair. Crucially, the preamble is **data-driven from the 02 fingerprint**: it states the user's measured habits as positive targets, not just a generic list of AI avoids. This is what turns "not like AI" into "like this user."

## Scope
- `build_style_preamble(fingerprint, config) -> str`:
  - **Positive targets from the fingerprint** (the new part): e.g. "This writer's voice, measured from their own work: mean sentence length ~14 words with high variance; prefers plain `is`/`has` over `serves as`; rarely opens with `Additionally`; uses em-dashes freely; sentence-case headings; concrete and specific over abstract; hedges with '<the user's actual hedges>'." Render only features with sufficient fingerprint support.
  - **Avoids from the registry**, but *minus the features the user genuinely uses* (don't tell a tapestry-user to avoid "tapestry"). De-duplicated, grouped, length-capped.
- Inject into `creek/generate/drafts.py` `_compose_prompt()` as a `## Voice targets` section (after voice-core, alongside the skill stack), and into the bare-section and stitch prompts.
- Config: `AIStyleConfig.prevent_in_prompt: bool = True`, length cap, and `include_measured_targets: bool = True`.

## Out of scope
The post-generation guard/rewrite (09). Detection logic (05–07). The fingerprint computation (02).

## Design constraints
- Preamble derived from fingerprint + registry, never hand-copied, so it tracks both the catalog and the specific user.
- Keep it short and favour positive framing; an overlong avoid-list induces the stilted "evasion" voice we're trying to escape.
- Compose cleanly with voice-core + skill stack + source material (no FEAT-032/grounding regression — assert these survive).
- If the fingerprint is thin, fall back to the generic avoids + a "voice profile is preliminary" note rather than fabricating targets.

## Files to touch
- new: `creek/generate/ai_style/preamble.py`
- edit: `creek/generate/drafts.py` `_compose_prompt()` (+ bare/stitch builders); `AIStyleConfig`
- tests: `tests/generate/ai_style/test_preamble.py` (measured-targets rendered from a fixture fingerprint; a user's genuine word is NOT in the avoid list); draft-prompt regression test (voice-core + source intact)

## Acceptance criteria
- Preamble contains fingerprint-derived positive targets and a registry avoid-list that excludes the user's genuine features; length-capped; present in all three prompt paths when enabled.
- Thin-fingerprint fallback path tested. Existing draft tests still pass. check-all.sh green; ≥90% cov.

## Est. LOC
~300–450. Depends on: 01, 02 (most useful after 05–07 populate the catalog).
