FEAT-040.7: Discourse/structure detectors — parallelisms, rule-of-three, outline sections, disclaimers, comm boilerplate (vault-relative)

## Context
Detection-only, completing the substantive catalog. Surface (feed prevention/rewrite); never auto-strip. Several are comment-context only (honour the 01 `context` flag). Suppression is fingerprint-driven.

## Scope (per the guide)
- **Negative parallelisms** (WP:AIPARALLEL): `not only … but …`, `it's not …, it's …`, `not X, but Y`, `no …, no …, just …`, and the multi-sentence "however, …took a path that intertwined…" form.
- **Rule of three** (WP:RO3): triads used as padding — **strongly fingerprint-gated**, since triads are common in good human prose. Flag only when the draft's triad rate exceeds the user's measured `rule_of_three_rate` and clusters with other tells.
- **"Challenges / Future Prospects" outline sections**: the rigid `Despite its <puff>, <subject> faces challenges…` + speculative-future close formula (not the mere mention of challenges).
- **List/broad-title-as-proper-noun leads**: defining a list/non-proper-noun title as a standalone entity.
- **Knowledge-cutoff & "not documented" disclaimers** (WP:AICUTOFF): `As of my last knowledge update`, `While specific details are limited in the available sources`, `likely supports…`, `maintains a low profile` — high severity + fabrication-risk hint.
- **Didactic disclaimers** (WP:DIDACTIC): `it's important to note`, `worth noting`, `may vary`.
- **Section summaries** (WP:INCONCLUSION): `In summary,`/`In conclusion,`/`Overall,` restating the thesis.
- **Collaborative-comm boilerplate** (comments): `I hope this helps`, `Certainly!`, `Would you like…`, AfC submission statements, `Subject:` lines, "let's focus on content not conduct".

## Out of scope
Auto-removal. Lexical (05) and significance/puffery (06). The preamble itself (08).

## Design constraints
- **Surface, don't strip**; **fingerprint-gated** rate comparisons (esp. rule-of-three and parallelisms — if the user loves a triad, it's voice).
- Comment-only tells gated by `context`.
- Cutoff/"not documented" disclaimers carry the fabrication-risk hint regardless of fingerprint (these are never desirable in finished prose).

## Files to touch
- new: `creek/generate/ai_style/discourse.py`, optional phrase data in `data/`
- tests: `tests/generate/ai_style/test_discourse.py` (Deadbot rule-of-three, Panama-Canal Challenges, List-of-songs lead, Chester cutoff, Eugenio-Duse parallelism positives; a user who genuinely uses triads as a negative that must not flag)

## Acceptance criteria
- Each pattern detected on its guide example; rule-of-three suppressed for a triad-loving user fingerprint; comment-only tells gated; cutoff findings carry the hint.
- Precision recorded on the user's corpus. check-all.sh green; ≥90% cov.

## Est. LOC
~550–700. Depends on: 01, 02.
