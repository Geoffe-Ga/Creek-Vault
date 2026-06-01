FEAT-040.9: Voice-fidelity guard + revision loop wired into `creek draft`

## Context
The integration issue: during `creek draft`, sanitize → measure distance to the user's voice fingerprint → run a bounded rewrite that *moves the draft toward the user's voice* → stamp the result into frontmatter. Mirrors `creek/generate/grounding.py`. The objective is minimising voice distance, not zeroing a tell checklist.

## Scope
- `creek/generate/ai_style/guard.py` `run_voice_fidelity_guard(body, *, fingerprint, config, llm, no_llm) -> VoiceFidelityReport`:
  1. **Sanitize** (03–04) — mechanical/markup fixed; typography normalised only against the user's grain.
  2. **Scan** (05–07) against the fingerprint → `ScanReport` with `voice_distance` + directional findings (over/under-use).
  3. If `voice_distance > config.voice_distance_upper` and not `no_llm`: run **one** targeted-rewrite pass given the flagged spans, the directional deltas, and the fingerprint-derived voice targets, instructed to revise *toward the user's measured voice* (plain copulas if the user is plain, restore their sentence rhythm, cut puffery the user doesn't use, de-pad triads) **without inventing facts or dropping grounded content**. Re-sanitize + re-scan. Cap at `config.max_revision_passes` (default 1–2).
  4. Return final body + `voice_distance` + structured `voice_findings`.
- Stamp `voice_distance` / `voice_findings` into draft frontmatter; add `build_voice_fidelity_frontmatter()` mirroring `build_grounding_frontmatter()`.
- Wire into `DraftGenerator.save_draft()` + `save_outline_draft()` (the `_draft_file_body()` path); honour `--no-llm` (sanitize + measure only) and `AIStyleConfig.enabled`. Emit a stderr line: `voice-fidelity: distance 0.22 (3 residual divergences) — see frontmatter`.

## Out of scope
The lint audit pass (10). New detectors/sanitizers (03–07). Fingerprint build (02).

## Design constraints
- **Bounded, deterministic-first, regression-guarded:** keep the lower-distance version; if a rewrite raises distance, discard it.
- **Never trade grounding for voice:** re-run the existing grounding guard after rewrite; reject a rewrite that drops below the grounding floor (compose with FEAT-032, don't fight it).
- **Move toward voice, not toward emptiness:** the rewrite targets the *deeper* problem and the *directional* deltas (including under-use — restore the user's characteristic moves), not cosmetic word-swaps.
- Frontmatter-safe; idempotent on re-save; thin-fingerprint softening from 01.

## Files to touch
- new: `creek/generate/ai_style/guard.py`
- edit: `creek/generate/drafts.py` (save paths, stderr summary), `creek/generate/ai_style/__init__.py`, `AIStyleConfig` (`voice_distance_upper`, `max_revision_passes`, `enabled`)
- tests: `tests/generate/ai_style/test_guard.py` (stub LLM) + draft save-path tests: frontmatter stamping, `--no-llm`, iteration cap, lower-distance-wins, grounding-floor rejection

## Acceptance criteria
- An AI-tropey body is sanitized, measured, rewritten once (stub LLM) toward a fixture fingerprint, re-measured lower, saved with `voice_distance`/`voice_findings`.
- `--no-llm`: sanitize + measure only, no network. Distance-raising rewrite discarded; grounding-dropping rewrite rejected. check-all.sh green; ≥90% cov.

## Est. LOC
~550–700. Depends on: 01, 02, 03, 04, and ≥1 of 05–07. 08 recommended first.
