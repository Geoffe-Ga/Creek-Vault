## Role

You are a senior Python engineer working across `creek-tools/creek/classify/` and the ingest path,
fluent in how fragments are tagged with frequency / wavelength / primitives.

## Goal

Ensure content ingested under `11-Other-Authors/` gets its **ideas** fully classified into the
ontology (frequency, wavelength phase/mode/orientation, threads/eddies) while its **attribution**
is set correctly: `source.author ∈ {other, ai}`, `source.author_slug` = the author folder, a
`representativeness` value derived from the author manifest, and a fail-closed `voice_weight=0.0`.

## Context

- **Parent epic:** #EPIC_01_NUMBER
- **Predecessor issue(s):** #EPIC_01_ISSUE_02_NUMBER (manifest), #EPIC_01_ISSUE_03_NUMBER (fields), #EPIC_01_ISSUE_04_NUMBER (exclusion).
- **SPEC section:** §7.3 (idea classification), §7.4 (attribution).
- **Files involved:**
  - `creek-tools/creek/classify/` and/or the ingest writer — apply attribution when the destination path is under `11-Other-Authors/`.
  - `creek-tools/creek/vault/writer.py` — stamp `author_slug` / `voice_weight` / `representativeness` from the governing `_author.md`.
  - `creek-tools/tests/` — classification + attribution tests.
- **Prior decisions:** Classification is identical to native fragments (full ontology tags) — only attribution differs. `ai_as_user` → `source.author = ai`, `representativeness = endorsed`; `human_source` → `source.author = other`, `representativeness` from manifest (`reference`/`aspirational`). `voice_weight` always inherits the manifest (default 0.0).
- **State of the world:** Classifier tags native fragments today; it has no special handling for `11-Other-Authors/`. The manifest loader + fields + exclusion now exist.

## Output Format

A single PR containing:

- [ ] Attribution stamping when writing under `11-Other-Authors/`, sourced from the governing `_author.md`.
- [ ] Full ontology classification still applied to the ideas.
- [ ] Tests: a `human_source` work gets `source.author=other`, full frequency/phase tags, `voice_weight=0.0`; an `ai-as-user` piece gets `source.author=ai`, `representativeness=endorsed`.
- [ ] Fail-closed test: a work whose `_author.md` is missing/unreadable still lands with `voice_weight=0.0`.

## Examples

```python
frag = ingest_into(vault / "11-Other-Authors/naval-ravikant/almanack", text)
assert frag.source.author == "other"
assert frag.source.author_slug == "naval-ravikant"
assert frag.voice_weight == 0.0
assert frag.frequency.primary in {f"F{i}" for i in range(1, 11)}  # ideas still classified
```

## Constraints

**Scope fence:** Do not build retrieval over this content (EPIC_02). Do not add new ingest source
types — only attribution behavior for the existing path.

**Anti-bypass (verbatim, non-negotiable):**

> No `noqa`, `# type: ignore`, `pylint: disable`, `eslint-disable`, or equivalent
> linter/type-checker silencers. Fix the root cause. The only exception is the documented 4-line
> escape hatch (third-party library bug / language-version compatibility / benchmarked
> performance necessity / generated code) — and it must include the reason, a reference URL, an
> alternative considered, and a review date. See the `max-quality-no-shortcuts` skill.

**Tracer-code invariant:** Ingesting native (non-`11-Other-Authors/`) content is unchanged.

## Definition of Done (stay-green)

- [ ] All new and existing tests pass (`cd creek-tools && ./scripts/test.sh --all`).
- [ ] `pre-commit run --all-files -c creek-tools/.pre-commit-config.yaml` is clean.
- [ ] Coverage on changed lines ≥90% branch; docstrings ≥95%; complexity ≤10; MyPy strict.
- [ ] Fail-closed attribution proven by test.
- [ ] PR body includes `Refs #EPIC_01_NUMBER` and `Closes #THIS_ISSUE_NUMBER`.
- [ ] Latest Claude reviewer `Verdict:` on HEAD is `LGTM`.

## Labels

`spec-decomposition`, `edges`, `classify`
