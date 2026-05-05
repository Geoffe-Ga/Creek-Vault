# INC-003: Privacy tier naming divergence — code uses `public`, docs use `open`

**Severity:** Medium
**Category:** INC
**Estimated complexity:** S (≤2h)
**Parallelizable with peers in same category:** yes
**Discovered by:** Reading dimension 5; confirmed by parallel agent

## Files affected
- `creek/models.py:199-209` — `PrivacyTier.PUBLIC = "public"`
- `creek/classify/privacy.py` — uses `PrivacyTier.PUBLIC`
- `creek-tools/docs/classification.md:38, 94-100` — table uses "open"
- `00-Creek-Meta/Ontology/creek_ontology_agent_prompt.md:1244` — table uses "Open"

## Dependencies
None. Pairs with INC-007 (`--include-tier`) so the user-facing string is consistent.

## Blockers
None.

## Reproduction
```bash
grep -n '"public"\|PUBLIC' creek/models.py creek/classify/privacy.py | head
# many hits
grep -rn "open\|public" creek-tools/docs/classification.md
# docs say "open"
```

## Analysis

The ontology spec and user-facing docs use `Open`. The Pydantic model and every code reference use `public`. They mean the same thing, but a user editing frontmatter by hand based on the docs will write `tier: open` and Pydantic validation will fail (or, with `use_enum_values=True`, silently mismatch every comparison).

Pick one:
- Keep `public` (already in code; smaller change), update three doc files.
- Switch to `open` (matches spec; better intent), rename enum value, search/replace.

Either way, do it before launch — once written into vaults in the wild, migration becomes harder.

Also: the docs use lowercase strings (`open | personal | intimate`) but the code's enum names (PUBLIC, PERSONAL, INTIMATE, UNCLASSIFIED) are uppercase. With `use_enum_values=True` the *serialized* form is lowercase, so this is fine — just confirm it across every place that compares.

Confidence: verified.

## Proposed remediation

Pick `open`. Reasons:
- Matches the canonical spec (ontology §13.2).
- "Open" connotes "openly publishable"; "public" connotes "internet-public", which is too aggressive for a personal-knowledge tool.
- The docs are already aligned on `open`; only the model needs to change.

Migration: add a Pydantic field validator that accepts both `open` and `public` (mapping the latter to `open`) for one release, log a deprecation warning, then remove the alias.

## Acceptance criteria

- `PrivacyTier.OPEN = "open"` in `creek/models.py`.
- All comparisons that previously checked `PrivacyTier.PUBLIC` use `PrivacyTier.OPEN`.
- A pre-existing fragment with `privacy_tier: public` loads successfully (with a warning log) for one release.
- The `docs/` are unchanged; they were already correct.
- A test asserts that `PrivacyTier("open")` works and `PrivacyTier("public")` works *only* via the alias path, with the alias path emitting a deprecation warning.

## References
- `creek/models.py:199-209`
- `creek-tools/docs/classification.md:38, 94-100`
- `00-Creek-Meta/Ontology/creek_ontology_agent_prompt.md:1244`
