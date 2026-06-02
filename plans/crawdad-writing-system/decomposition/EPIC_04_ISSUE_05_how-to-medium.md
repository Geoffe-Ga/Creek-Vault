## Role

You are a senior Python engineer working in the medium skill tree, fluent in Technical fragments,
Praxis, and structured/example-driven technical writing.

## Goal

Add the `how-to` medium contract — agentic-coding guides that weight the Graph + Ontology agents
toward Technical fragments and Praxis, produce structured, example-driven output, and add a
reflection check for accuracy/runnability.

## Context

- **Parent epic:** #EPIC_04_NUMBER
- **Predecessor issue(s):** #EPIC_04_ISSUE_04_NUMBER (book-report).
- **SPEC section:** §5 (medium #6 `how-to`), §8 (accuracy/runnability reflection check).
- **Files involved:**
  - `creek-tools/creek/templates/skills/mediums/how-to.MEDIUM.md`.
  - `creek-tools/tests/` — how-to medium test.
- **Prior decisions:** Weight `01-Fragments/Technical/` + `04-Praxis/`; structured, example-driven; reflection adds an accuracy/runnability dimension.
- **State of the world:** All prior mediums + the desk exist; this completes the medium set.

## Output Format

A single PR containing:

- [ ] `how-to.MEDIUM.md` contract (passes conformance harness).
- [ ] Test: a how-to draft is structured + example-driven; reflection includes a runnability check.

## Examples

```bash
creek author --medium how-to --topic "wiring an MCP verb in creek-tools" --vault ~/Creek
```

## Constraints

**Scope fence:** Only `how-to`. No desk-internal changes.

**Anti-bypass (verbatim, non-negotiable):**

> No `noqa`, `# type: ignore`, `pylint: disable`, `eslint-disable`, or equivalent
> linter/type-checker silencers. Fix the root cause. The only exception is the documented 4-line
> escape hatch (third-party library bug / language-version compatibility / benchmarked
> performance necessity / generated code) — and it must include the reason, a reference URL, an
> alternative considered, and a review date. See the `max-quality-no-shortcuts` skill.

**Tracer-code invariant:** Existing mediums unaffected; the medium set is complete and demoable.

## Definition of Done (stay-green)

- [ ] All new and existing tests pass (`cd creek-tools && ./scripts/test.sh --all`).
- [ ] `pre-commit run --all-files -c creek-tools/.pre-commit-config.yaml` is clean.
- [ ] Coverage on changed lines ≥90% branch; docstrings ≥95%; complexity ≤10; MyPy strict.
- [ ] PR body includes `Refs #EPIC_04_NUMBER` and `Closes #THIS_ISSUE_NUMBER`.
- [ ] Latest Claude reviewer `Verdict:` on HEAD is `LGTM`.

## Labels

`spec-decomposition`, `core`, `mediums`
