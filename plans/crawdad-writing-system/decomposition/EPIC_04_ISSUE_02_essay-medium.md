## Role

You are a senior Python engineer working in the medium skill tree, fluent in the FEAT-040 voice
skills and the reflection rubric.

## Goal

Add the `essay` medium contract — long-form, thought-provoking Substack-style writing — weighting
the Voice + Retrieval agents and a reflection rubric that prioritizes voice fidelity and narrative
coherence.

## Context

- **Parent epic:** #EPIC_04_NUMBER
- **Predecessor issue(s):** #EPIC_04_ISSUE_01_NUMBER (conformance harness + chat).
- **SPEC section:** §5 (medium #3 `essay`), §8 (voice-fidelity-weighted rubric).
- **Files involved:**
  - `creek-tools/creek/templates/skills/mediums/essay.MEDIUM.md`.
  - `creek-tools/tests/` — essay medium test.
- **Prior decisions:** Essay weights Voice + Retrieval; reflection prioritizes voice fidelity + coherence over citation density. Drafts may seed `07-Voice/Drafts/`.
- **State of the world:** `research` + `chat` exist; the harness validates contracts.

## Output Format

A single PR containing:

- [ ] `essay.MEDIUM.md` contract (passes the conformance harness).
- [ ] Test: an essay draft on the fixture is long-form, voiced, and coherent; reflection weights voice fidelity highest.

## Examples

```python
draft = run_author(medium="essay", topic="leverage vs. presence", vault=fixture)
assert draft.contract.reflection_weights["voice_fidelity"] == max(draft.contract.reflection_weights.values())
```

## Constraints

**Scope fence:** Only `essay`. No desk-internal changes.

**Anti-bypass (verbatim, non-negotiable):**

> No `noqa`, `# type: ignore`, `pylint: disable`, `eslint-disable`, or equivalent
> linter/type-checker silencers. Fix the root cause. The only exception is the documented 4-line
> escape hatch (third-party library bug / language-version compatibility / benchmarked
> performance necessity / generated code) — and it must include the reason, a reference URL, an
> alternative considered, and a review date. See the `max-quality-no-shortcuts` skill.

**Tracer-code invariant:** Existing mediums unaffected; one new medium added via contract only.

## Definition of Done (stay-green)

- [ ] All new and existing tests pass (`cd creek-tools && ./scripts/test.sh --all`).
- [ ] `pre-commit run --all-files -c creek-tools/.pre-commit-config.yaml` is clean.
- [ ] Coverage on changed lines ≥90% branch; docstrings ≥95%; complexity ≤10; MyPy strict.
- [ ] PR body includes `Refs #EPIC_04_NUMBER` and `Closes #THIS_ISSUE_NUMBER`.
- [ ] Latest Claude reviewer `Verdict:` on HEAD is `LGTM`.

## Labels

`spec-decomposition`, `core`, `mediums`
