FEAT-040.6: Rhetorical detectors — significance/legacy/trend, superficial analysis, puffery, weasel attribution (vault-relative)

## Context
Detection-only for the *substantive* tells. These point at deeper problems (ungrounded synthesis, padding, opinion-as-fact) and are never auto-stripped — they feed prevention (08), targeted rewrite (09), and surfacing (10). Suppression is fingerprint-driven: a user who genuinely writes in a grand, legacy-minded register should not be flagged for doing so.

## Scope (per the guide)
- **Undue emphasis on significance / legacy / broader trends** (WP:AILEGACY/WP:AITREND): `marking a pivotal moment in the evolution of…`, `represents a significant shift`, `contributing to the broader…`, `stands as a testament`, `enduring legacy`, the biology over-framing variant, and hedged-preamble-then-puff.
- **Superficial analyses** (WP:SUPERFICIAL): trailing `-ing` clauses (`…, highlighting/underscoring/reflecting…`), incl. the RAG "Roger Ebert highlighted the lasting influence" variant; "has generated debate about X, Y, and Z".
- **Promotional / peacock language** (WP:AIPUFFERY): `boasts`, `vibrant`, `nestled`, `in the heart of`, travel-guide/press-release register; the cultural-heritage-importance reminder subtype.
- **Vague attribution & overgeneralisation** (WP:AIWEASEL): `Observers have cited`, `Experts argue`, `several sources` while citing one; one source presented as widely held.

## Out of scope
Auto-removal. Parallelisms/rule-of-three/structure (07). Lexical density (05).

## Design constraints
- **Surface, don't strip**; output the deeper-problem hint (e.g. "trailing `-ing` clause = likely unattributed synthesis → reground or cut").
- **Vault-relative suppression:** compare the draft's rate of these constructions to the user's measured rate (the fingerprint tracks `superficial_ing_rate`, `significance_phrase_rate`, etc.). A user whose own essays run grand does not get flagged for grandeur; the signal is the *delta*.
- Down-weight when a real inline citation is attached (compose with 11 when available); until then, low severity + caveat.
- Bias to precision: over-flagging substantive prose erodes trust. Negative set = the user's own fragments.

## Files to touch
- new: `creek/generate/ai_style/rhetorical.py`, optional phrase data in `data/`
- tests: `tests/generate/ai_style/test_rhetorical.py` (Idescat, Douéra, McAllen-Temple, Cadillac-Sollei AI positives; user's own feature-prose fragments as negatives that must not flag)

## Acceptance criteria
- AI examples flag with `rhetorical` + deeper-problem hint; the user's genuine register (per a fixture fingerprint) does not flag above baseline; sourced "critics argued [cite]" down-weighted.
- Precision recorded on the user's corpus. check-all.sh green; ≥90% cov.

## Est. LOC
~550–700. Depends on: 01, 02.
