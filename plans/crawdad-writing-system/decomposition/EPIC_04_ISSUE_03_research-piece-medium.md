## Role

You are a senior Python engineer working in the medium skill tree, fluent in the attribution model
(`11-Other-Authors/`) and citation discipline.

## Goal

Add the `research-piece` medium contract — externally-facing analytical writing with heavier
citation discipline that may draw on `11-Other-Authors/` source material with explicit attribution.

## Context

- **Parent epic:** #EPIC_04_NUMBER
- **Predecessor issue(s):** #EPIC_04_ISSUE_02_NUMBER (essay) — and EPIC_01 attribution model.
- **SPEC section:** §5 (medium #4 `research-piece`), §7.4 (attribution), §8 (attribution-correctness gate).
- **Files involved:**
  - `creek-tools/creek/templates/skills/mediums/research-piece.MEDIUM.md`.
  - `creek-tools/tests/` — attribution-aware citation test.
- **Prior decisions:** Heavier citation than essay; borrowed ideas attributed to their `11-Other-Authors/` author; the owner's voice never claims a `reference`-weighted idea as its own.
- **State of the world:** Attribution model (EPIC_01) and desk (EPIC_02) exist; mediums research/chat/essay exist.

## Output Format

A single PR containing:

- [ ] `research-piece.MEDIUM.md` contract (passes conformance harness).
- [ ] Test: a piece drawing on an `11-Other-Authors/` work cites that author; reflection flags a missing attribution.

## Examples

```python
draft = run_author(medium="research-piece", topic="specific knowledge", vault=fixture_with_naval)
assert any(c.attributed_to == "naval-ravikant" for c in draft.claims)
```

## Constraints

**Scope fence:** Only `research-piece`. No desk-internal changes.

**Anti-bypass (verbatim, non-negotiable):**

> No `noqa`, `# type: ignore`, `pylint: disable`, `eslint-disable`, or equivalent
> linter/type-checker silencers. Fix the root cause. The only exception is the documented 4-line
> escape hatch (third-party library bug / language-version compatibility / benchmarked
> performance necessity / generated code) — and it must include the reason, a reference URL, an
> alternative considered, and a review date. See the `max-quality-no-shortcuts` skill.

**Tracer-code invariant:** Existing mediums unaffected; attribution correctness exercised end-to-end.

## Definition of Done (stay-green)

- [ ] All new and existing tests pass (`cd creek-tools && ./scripts/test.sh --all`).
- [ ] `pre-commit run --all-files -c creek-tools/.pre-commit-config.yaml` is clean.
- [ ] Coverage on changed lines ≥90% branch; docstrings ≥95%; complexity ≤10; MyPy strict.
- [ ] Attribution-correctness proven by test.
- [ ] PR body includes `Refs #EPIC_04_NUMBER` and `Closes #THIS_ISSUE_NUMBER`.
- [ ] Latest Claude reviewer `Verdict:` on HEAD is `LGTM`.

## Labels

`spec-decomposition`, `core`, `mediums`
