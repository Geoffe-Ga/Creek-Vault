FEAT-040.3: Deterministic sanitizer — stray-markup artifacts (always) + typography (vault-aware)

## Context
First deterministic-repair issue. Splits cleanly by whether a feature can ever be the user's voice. Builds on the 01 framework and the 02 `VoiceFingerprint`.

## Scope
**Always strip (never anyone's voice):** `:contentReference[oaicite:N]{index=N}`, `oai_citation`/`oai_citation:N‡…`, `contentReference`, `Example+1`, `citeturn0search0`/`turn0(search|image|news|file)N` (incl. PUA-wrapped), `citeturn0news0`/`citeturn1file0`, lenticular `【85†L261-269】`, `[attached_file:1]`/`[web:1]`, `<grok-card …>`, `grok_render_citation_card_json={…}`, `({"attribution":{"attributableIndex":"X-Y"}})`, and tracking params `utm_source=chatgpt.com|openai|copilot.com` / `referrer=grok.com` (preserve the rest of the URL).

**Vault-aware typography (repair only against the user's grain):**
- Curly quotes/apostrophes (WP:AICURLY) and em-dash density (WP:AIDASH) are **style, not noise**. Consult the 02 fingerprint: if the user's own writing uses curly quotes / em-dashes at rate ≥ the draft, **leave them** — that's their voice. Only normalise toward straight quotes / lower dash density when the fingerprint shows the user does *not* write that way. When the fingerprint is thin/low-confidence, default to a gentle generic normalisation and flag rather than hard-rewrite.

## Out of scope
Markdown structural conversion / headings / placeholders (04). Lexical/rhetorical detection (05–07).

## Design constraints
- **Frontmatter-safe & idempotent.** Body only.
- The artifact strippers are unconditional and need negative fixtures (a real URL containing `search`, real lenticular brackets in CJK text). Typography normalisation is conditional on the fingerprint — add tests for both branches (user-uses-em-dashes → preserved; user-doesn't → normalised).

## Files to touch
- new: `creek/generate/ai_style/sanitize_typography.py`
- edit: `creek/generate/ai_style/__init__.py` (`sanitize(text, *, fingerprint, config)` aggregator, extended in 04)
- tests: `tests/generate/ai_style/test_sanitize_typography.py`

## Acceptance criteria
- All markup artifacts removed from the guide's examples; URLs survive param-stripping.
- Em-dash/curly-quote normalisation respects the fingerprint (two-branch test).
- Idempotent; frontmatter untouched. check-all.sh green; ≥90% cov.

## Est. LOC
~550–700. Depends on: 01, 02. Coordinate `sanitize()` with 04.
