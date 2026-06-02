## Role

You are a senior Python engineer working in `creek-tools/creek/models.py`, fluent in Pydantic v2
and this repo's frontmatter conventions.

## Goal

Add an `AuthorManifest` Pydantic model and a loader that parses a `11-Other-Authors/<slug>/_author.md`
file into it, validating the attribution fields and failing closed on malformed input.

## Context

- **Parent epic:** #EPIC_01_NUMBER
- **Predecessor issue(s):** #EPIC_01_ISSUE_01_NUMBER (scaffold + `_author.md` template must exist).
- **SPEC section:** §7.2 (author manifest), §7.4 (attribution axes).
- **Files involved:**
  - `creek-tools/creek/models.py` — new `AuthorManifest` model.
  - `creek-tools/creek/vault/` (reader/loader module) — `load_author_manifest(path) -> AuthorManifest`.
  - `creek-tools/tests/` — model + loader tests.
- **Prior decisions:** `author_kind ∈ {human_source, ai_as_user, collaborator}`; `representativeness ∈ {self, endorsed, aspirational, reference}`; `voice_weight: float` in `[0.0, 1.0]`. Fail closed: missing/invalid `voice_weight` → `0.0`; missing `representativeness` → `reference`.
- **State of the world:** The `_author.md` template exists from ISSUE_01; nothing parses it yet.

## Output Format

A single PR containing:

- [ ] `AuthorManifest` model with typed, validated fields and docstrings.
- [ ] `load_author_manifest()` loader with fail-closed defaults.
- [ ] Tests: valid manifest round-trips; malformed `voice_weight`/`author_kind` fail closed; missing file raises a clear error.

## Examples

```python
m = load_author_manifest(Path("11-Other-Authors/naval-ravikant/_author.md"))
assert m.author_slug == "naval-ravikant"
assert m.author_kind == "human_source"
assert m.voice_weight == 0.0          # fail-closed default if absent
assert m.representativeness == "reference"
```

A manifest declaring `voice_weight: "banana"` must not raise an uncaught error — it resolves to
`0.0` (fail closed) and logs a warning.

## Constraints

**Scope fence:** Do not wire this into classification or voice generation yet (ISSUE_04/05). Model
+ loader + tests only.

**Anti-bypass (verbatim, non-negotiable):**

> No `noqa`, `# type: ignore`, `pylint: disable`, `eslint-disable`, or equivalent
> linter/type-checker silencers. Fix the root cause. The only exception is the documented 4-line
> escape hatch (third-party library bug / language-version compatibility / benchmarked
> performance necessity / generated code) — and it must include the reason, a reference URL, an
> alternative considered, and a review date. See the `max-quality-no-shortcuts` skill.

**Tracer-code invariant:** The system remains demoable; no existing model changes behavior.

## Definition of Done (stay-green)

- [ ] All new and existing tests pass (`cd creek-tools && ./scripts/test.sh --all`).
- [ ] `pre-commit run --all-files -c creek-tools/.pre-commit-config.yaml` is clean.
- [ ] Coverage on changed lines ≥90% branch; docstring coverage ≥95%; complexity ≤10; MyPy strict.
- [ ] PR body includes `Refs #EPIC_01_NUMBER` and `Closes #THIS_ISSUE_NUMBER`.
- [ ] Latest Claude reviewer `Verdict:` on HEAD is `LGTM`.

## Labels

`spec-decomposition`, `core`, `vault`
