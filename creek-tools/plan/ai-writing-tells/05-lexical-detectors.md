FEAT-040.5: Lexical detectors — vault-relative AI-vocab, copulatives, transitions, "concrete"

## Context
Detection-only. The candidate word lists come from the guide; **the thresholds come from the 02 fingerprint.** A word is a tell for *this* user only if the draft uses it at a rate the user's own writing doesn't support. Feeds the idiolect preamble (08), the voice-fidelity guard (09), and the lint check (10).

## Scope (per the guide, gated by the fingerprint)
- **AI-vocabulary** (WP:AIVOCAB): ship the guide's era-bucketed list as candidates, but flag a word only when `draft_rate(word) > user_rate(word) + margin`. If the user genuinely writes "tapestry"/"underscore"/"vibrant", those do not flag. Score by co-occurrence density of *over-the-user-baseline* words. Report the dominant era bucket among the over-baseline hits.
- **Copulative avoidance**: flag `serves as`/`stands as`/`boasts`/`features`/`offers`/`maintains`/lead `refers to` only when the draft's marketing-verb-vs-copula ratio exceeds the user's measured ratio. (Some users genuinely write "serves as".)
- **Sentence-initial transition overuse**: `Additionally,`/`Moreover,`/`Furthermore,`/`Notably,` flagged only above the user's measured opener rate.
- **"concrete"** (WP:CONCRETE, comments context only).

## Out of scope
Rewriting/synonym substitution. Rhetorical/discourse tells (06–07). Mechanical fixes (03–04).

## Design constraints
- **Vault-relative thresholds everywhere** — the per-word `user_rate` from the fingerprint *is* the false-positive layer; no hand-written caveat lists.
- Thin-fingerprint fallback (from 01): soften to a conservative generic band and lower severity when the user's per-word support is sparse.
- Context-gate literal senses still applies for words the fingerprint can't disambiguate (`underscore`=music, `key`=physical key).
- Density/rate-based; one hit never trips the score.

## Files to touch
- new: `creek/generate/ai_style/lexical.py`, `data/ai_vocabulary.yaml`
- edit: `AIStyleConfig` (margins, era toggle)
- tests: `tests/generate/ai_style/test_lexical.py` — **headline test:** a fingerprint built from a user who uses "tapestry" suppresses the flag, while the Somali-cuisine AI example (tapestry×N over a baseline-zero user) flags.

## Acceptance criteria
- Same word flags or not depending on the fingerprint (two-branch test); AI examples over baseline flag; user's genuine vocabulary does not.
- Calibration precision recorded against the user's own fragments (01 harness). check-all.sh green; ≥90% cov.

## Est. LOC
~500–700. Depends on: 01, 02.
