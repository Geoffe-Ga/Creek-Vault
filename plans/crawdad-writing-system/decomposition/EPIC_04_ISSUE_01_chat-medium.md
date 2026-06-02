## Role

You are a senior Python engineer working in `creek/author/` and the medium skill tree, fluent in the
FEAT-015 conversational loop.

## Goal

Add the `chat` medium contract — re-homing the FEAT-015 conversational reply behavior as a medium —
plus a medium-contract **conformance test harness**. This is the skeleton that proves the contract
abstraction generalizes beyond `research`.

## Context

- **Parent epic:** #EPIC_04_NUMBER
- **Predecessor issue(s):** none — skeleton issue for EPIC_04. (Depends on EPIC_02 `MediumContract` + desk.)
- **SPEC section:** §5 (medium #2 `chat`), §8 (per-medium rubric weighting).
- **Files involved:**
  - `creek-tools/creek/templates/skills/mediums/chat.MEDIUM.md` — contract.
  - `creek-tools/creek/author/` — allow `--medium chat`; low-latency path may bypass full retrieval for trivial turns.
  - `creek-tools/tests/` — conformance harness + chat tests.
- **Prior decisions:** `chat` is the simplest medium: short voiced reply, light citation, may skip full retrieval. The conformance harness validates any contract (budget, required rubric keys, specialist weights sum sanity).
- **State of the world:** Only `research` exists. The desk + contract model are in place.

## Output Format

A single PR containing:

- [ ] `chat.MEDIUM.md` contract within budget.
- [ ] `--medium chat` path (re-homed FEAT-015 behavior).
- [ ] A reusable conformance harness asserting any medium contract is well-formed.
- [ ] Tests: chat produces a short voiced reply; harness passes `research` + `chat`, fails a malformed contract.

## Examples

```python
assert_contract_conformant(load_medium_contract("chat", vault))
reply = run_author(medium="chat", query="hey crawdad, quick gut-check on F4?", vault=fixture)
assert len(reply.rendered_text) < CHAT_MAX_CHARS
```

## Constraints

**Scope fence:** Only `chat`. Do not add essay/research-piece/book-report/how-to (later issues).

**Anti-bypass (verbatim, non-negotiable):**

> No `noqa`, `# type: ignore`, `pylint: disable`, `eslint-disable`, or equivalent
> linter/type-checker silencers. Fix the root cause. The only exception is the documented 4-line
> escape hatch (third-party library bug / language-version compatibility / benchmarked
> performance necessity / generated code) — and it must include the reason, a reference URL, an
> alternative considered, and a review date. See the `max-quality-no-shortcuts` skill.

**Tracer-code invariant:** The desk now supports two mediums via one contract abstraction; research
is unaffected.

## Definition of Done (stay-green)

- [ ] All new and existing tests pass (`cd creek-tools && ./scripts/test.sh --all`).
- [ ] `pre-commit run --all-files -c creek-tools/.pre-commit-config.yaml` is clean.
- [ ] Coverage on changed lines ≥90% branch; docstrings ≥95%; complexity ≤10; MyPy strict.
- [ ] Conformance harness reused by later medium issues.
- [ ] PR body includes `Refs #EPIC_04_NUMBER` and `Closes #THIS_ISSUE_NUMBER`.
- [ ] Latest Claude reviewer `Verdict:` on HEAD is `LGTM`.

## Labels

`spec-decomposition`, `tracer-skeleton`, `mediums`
