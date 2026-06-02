## Role

You are a senior Python engineer working in `creek/author/` and the schema-skill tree, fluent in
this repo's `≤1500-token` skill-budget lint discipline.

## Goal

Introduce a `MediumContract` model and the first concrete contract — `research` — declared as a
skill file under the medium skill tree, loaded by the Conductor to drive structure, specialist
weighting, citation norm, default privacy, and the reflection rubric.

## Context

- **Parent epic:** #EPIC_02_NUMBER
- **Predecessor issue(s):** #EPIC_02_ISSUE_01_NUMBER (skeleton desk + stub Conductor).
- **SPEC section:** §5 (medium skill tree — `research` first), §8 (reflection rubric weighting).
- **Files involved:**
  - `creek-tools/creek/models.py` — `MediumContract` model.
  - `creek-tools/creek/templates/skills/mediums/research.MEDIUM.md` — deployed contract.
  - `creek-tools/creek/author/conductor.py` — load + apply the contract.
  - `creek-tools/creek/lint` path — extend the skill lint to cover `mediums/`.
  - `creek-tools/tests/` — contract load + lint tests.
- **Prior decisions:** Mediums live at `00-Creek-Meta/Skills/mediums/<medium>.MEDIUM.md`, deployed from templates, same ≤1500-token budget as schema skills. The `research` rubric prioritizes ontological accuracy + citation completeness over voice flourish.
- **State of the world:** The Conductor currently hard-codes the research path with stubs; there is no contract abstraction yet.

## Output Format

A single PR containing:

- [ ] `MediumContract` model (structure, specialist weights, citation norm, default privacy tier, reflection rubric).
- [ ] `research.MEDIUM.md` contract within the token budget; lint covers it.
- [ ] Conductor loads the contract and exposes its rubric to the (stub) Reflection node.
- [ ] Tests: contract loads + validates; lint fails a deliberately over-budget contract.

## Examples

```python
contract = load_medium_contract("research", vault)
assert contract.citation_norm == "every-claim"
assert contract.specialist_weights["graph"] >= contract.specialist_weights["voice"]
```

## Constraints

**Scope fence:** Only the `research` contract. Do not add `chat`/`essay`/etc. (EPIC_04). Do not make
specialists real (ISSUE_03/04).

**Anti-bypass (verbatim, non-negotiable):**

> No `noqa`, `# type: ignore`, `pylint: disable`, `eslint-disable`, or equivalent
> linter/type-checker silencers. Fix the root cause. The only exception is the documented 4-line
> escape hatch (third-party library bug / language-version compatibility / benchmarked
> performance necessity / generated code) — and it must include the reason, a reference URL, an
> alternative considered, and a review date. See the `max-quality-no-shortcuts` skill.

**Tracer-code invariant:** The desk still runs end-to-end on the fixture; the contract replaces the
hard-coded research path without breaking the pipeline.

## Definition of Done (stay-green)

- [ ] All new and existing tests pass (`cd creek-tools && ./scripts/test.sh --all`).
- [ ] `pre-commit run --all-files -c creek-tools/.pre-commit-config.yaml` is clean.
- [ ] Coverage on changed lines ≥90% branch; docstrings ≥95%; complexity ≤10; MyPy strict.
- [ ] PR body includes `Refs #EPIC_02_NUMBER` and `Closes #THIS_ISSUE_NUMBER`.
- [ ] Latest Claude reviewer `Verdict:` on HEAD is `LGTM`.

## Labels

`spec-decomposition`, `core`, `author-desk`
