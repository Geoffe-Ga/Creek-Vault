FEAT-040.4: Deterministic sanitizer — Markdown leakage, headings, breaks, placeholders, emoji

## Context
Second deterministic-repair issue (pairs with 03). These format tells are meaning-preserving to fix. One knob is vault-aware: heading case follows the user's measured habit from the 02 fingerprint.

## Scope (per the guide)
- **Markdown vs target markup** (WP:MARKDOWN): `**bold**`/`__bold__` and `*italic*`/`_italic_` → target emphasis or strip per config; `[text](url)` → target link form; unwrap whole-essay fenced code blocks (```` ```wikitext ````).
- **Headings** (WP:MARKDOWN, WP:AITITLECASE): `## H`/`### H` → `== H ==`/`=== H ===`; **case follows the fingerprint** — if the user writes sentence-case headings, downcase Title Case to sentence case (preserving proper nouns; flag-only when unsure); if the user's own habit is Title Case, leave it. Repair skipped heading levels and thematic breaks before headings (`----`/`---`/`***`).
- **Inline-header vertical lists** (WP:AILIST): normalise `- **Header:** text` / `1. **Header**: text` / `•`/`–`/`#`/emoji bullets; collapse to prose only when config opts in.
- **Emoji as formatting** (WP:AIEMOJI): strip decorative leading emoji from headings/bullets.
- **Phrasal templates & placeholders**: flag/strip `2025-xx-xx`, `access-date=2025-XX-XX`, `INSERT_SOURCE_URL_*`, `PASTE_*_HERE`, `[Your Name]`, `[Describe …]`, `↩`, and "Add ____" infobox comments.

## Out of scope
Typography/markup artifacts (03). Detection (05–07). Citation validation (11).

## Design constraints
- Frontmatter-safe, idempotent. Heading downcaser preserves proper nouns; flag-not-rewrite when low confidence.
- Heading-case target is read from the 02 fingerprint, not assumed.
- Placeholders that survive (`INSERT_*`/`PASTE_*`) are a hard fail the 09 guard must refuse to ship.

## Files to touch
- new: `creek/generate/ai_style/sanitize_structure.py`
- edit: `creek/generate/ai_style/__init__.py` (`sanitize()` composes 03 + 04)
- tests: `tests/generate/ai_style/test_sanitize_structure.py` (Villers-Chief, Navipet, Rotary-saw fixtures; + a sentence-case-habit vault and a title-case-habit vault to prove the heading branch follows the fingerprint)

## Acceptance criteria
- `## Geography`→`== Geography ==`; heading case follows fingerprint; thematic-break-before-heading removed; canned lists normalised; emoji stripped; placeholders flagged; whole-essay code fence unwrapped.
- Negative fixtures (real numbered list, proper-noun heading) survive. check-all.sh green; ≥90% cov.

## Est. LOC
~500–680. Depends on: 01, 02. Coordinate `sanitize()` with 03.
