## Role

You are a senior Python engineer working in the medium skill tree, fluent in threads/eddies and the
`11-Other-Authors/` attribution model.

## Goal

Add the `book-report` medium contract — synthesizes an `11-Other-Authors/` work against the owner's
existing threads/eddies ("how does this book resonate with what I already believe?").

## Context

- **Parent epic:** #EPIC_04_NUMBER
- **Predecessor issue(s):** #EPIC_04_ISSUE_03_NUMBER (research-piece).
- **SPEC section:** §5 (medium #5 `book-report`), §7 (`11-Other-Authors/`).
- **Files involved:**
  - `creek-tools/creek/templates/skills/mediums/book-report.MEDIUM.md`.
  - `creek-tools/tests/` — resonance-against-threads test.
- **Prior decisions:** Input is a specific `11-Other-Authors/<author>/<work>` path; output relates the work's ideas to the owner's threads/eddies, attributing the work throughout.
- **State of the world:** Attribution model + desk + prior mediums exist.

## Output Format

A single PR containing:

- [ ] `book-report.MEDIUM.md` contract (passes conformance harness).
- [ ] `--work <path>` accepted for this medium.
- [ ] Test: a book report on a fixture work references the owner's threads/eddies and attributes the work.

## Examples

```bash
creek author --medium book-report --work 11-Other-Authors/naval-ravikant/almanack --vault ~/Creek
```

## Constraints

**Scope fence:** Only `book-report`. No desk-internal changes.

**Anti-bypass (verbatim, non-negotiable):**

> No `noqa`, `# type: ignore`, `pylint: disable`, `eslint-disable`, or equivalent
> linter/type-checker silencers. Fix the root cause. The only exception is the documented 4-line
> escape hatch (third-party library bug / language-version compatibility / benchmarked
> performance necessity / generated code) — and it must include the reason, a reference URL, an
> alternative considered, and a review date. See the `max-quality-no-shortcuts` skill.

**Tracer-code invariant:** Existing mediums unaffected.

## Definition of Done (stay-green)

- [ ] All new and existing tests pass (`cd creek-tools && ./scripts/test.sh --all`).
- [ ] `pre-commit run --all-files -c creek-tools/.pre-commit-config.yaml` is clean.
- [ ] Coverage on changed lines ≥90% branch; docstrings ≥95%; complexity ≤10; MyPy strict.
- [ ] PR body includes `Refs #EPIC_04_NUMBER` and `Closes #THIS_ISSUE_NUMBER`.
- [ ] Latest Claude reviewer `Verdict:` on HEAD is `LGTM`.

## Labels

`spec-decomposition`, `core`, `mediums`
