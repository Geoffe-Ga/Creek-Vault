## Role

You are a senior Python engineer working in `creek-tools/creek/models.py` and the vault
writer/reader, fluent in this repo's `Fragment` frontmatter schema and its backward-compat rules.

## Goal

Add the two-axis attribution fields to `Fragment` — `source.author_slug: str | None`,
`voice_weight: float = 1.0`, and `representativeness: Literal["self","endorsed","aspirational","reference"] = "self"`
— and ensure they round-trip through the markdown writer/reader without disturbing existing vaults.

## Context

- **Parent epic:** #EPIC_01_NUMBER
- **Predecessor issue(s):** #EPIC_01_ISSUE_02_NUMBER (manifest model establishes the enums).
- **SPEC section:** §9 (data-model & frontmatter changes), §7.4 (two-axis model).
- **Files involved:**
  - `creek-tools/creek/models.py` — extend `Fragment` / its `source` submodel.
  - `creek-tools/creek/vault/writer.py` (+ reader) — serialize/deserialize the new fields.
  - `creek-tools/tests/` — round-trip + backward-compat tests.
- **Prior decisions:** Defaults make existing fragments behave identically (`voice_weight=1.0`, `representativeness="self"`, `author_slug=None`). `source.author` already exists as `self|ai|other|collaborative` — reuse it; do not rename.
- **State of the world:** `Fragment` has `source.author`, `frequency`, `wavelength`, `privacy_tier`, `provenance`, etc. No attribution-weight fields yet.

## Output Format

A single PR containing:

- [ ] New fields on `Fragment` with defaults + docstrings.
- [ ] Writer/reader serialization of the new fields.
- [ ] Tests: a legacy fragment (no new fields) loads with native-fragment defaults; a fragment with the new fields round-trips byte-stably.

## Examples

```python
# Legacy fragment with no attribution block:
f = read_fragment(legacy_md)
assert f.voice_weight == 1.0
assert f.representativeness == "self"
assert f.source.author_slug is None

# New other-author fragment round-trips:
f2 = read_fragment(write_fragment(other_author_fragment))
assert f2.source.author_slug == "naval-ravikant"
assert f2.voice_weight == 0.0
```

## Constraints

**Scope fence:** Do not change the classifier or voice generation (ISSUE_04/05). Do not set the
values automatically here — only define + persist them. Setting them on ingest is ISSUE_05.

**Anti-bypass (verbatim, non-negotiable):**

> No `noqa`, `# type: ignore`, `pylint: disable`, `eslint-disable`, or equivalent
> linter/type-checker silencers. Fix the root cause. The only exception is the documented 4-line
> escape hatch (third-party library bug / language-version compatibility / benchmarked
> performance necessity / generated code) — and it must include the reason, a reference URL, an
> alternative considered, and a review date. See the `max-quality-no-shortcuts` skill.

**Tracer-code invariant:** Existing vaults must read and write unchanged; this is a purely
additive, backward-compatible schema change.

## Definition of Done (stay-green)

- [ ] All new and existing tests pass (`cd creek-tools && ./scripts/test.sh --all`).
- [ ] `pre-commit run --all-files -c creek-tools/.pre-commit-config.yaml` is clean.
- [ ] Coverage on changed lines ≥90% branch; docstrings ≥95%; complexity ≤10; MyPy strict.
- [ ] Backward-compat test proves legacy fragments are untouched.
- [ ] PR body includes `Refs #EPIC_01_NUMBER` and `Closes #THIS_ISSUE_NUMBER`.
- [ ] Latest Claude reviewer `Verdict:` on HEAD is `LGTM`.

## Labels

`spec-decomposition`, `core`, `vault`
