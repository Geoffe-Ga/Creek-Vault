## Role

You are a senior Python engineer working in `creek/author/reflection.py` and the Conductor's retry
loop, fluent in the FEAT-040.x voice-fidelity lint and the canonical ontology taxonomy.

## Goal

Make the **Reflection node** real: an LLM-as-judge that scores a draft against the research rubric
and returns `PASS | REVISE | ESCALATE` with structured findings; wire the Conductor to feed REVISE
findings back into a bounded retry (`max_author_rounds`, default 3), and to ESCALATE to a human when
retries are exhausted rather than shipping a sub-threshold draft.

## Context

- **Parent epic:** #EPIC_02_NUMBER
- **Predecessor issue(s):** #EPIC_02_ISSUE_05_NUMBER (real synthesis + voice to judge).
- **SPEC section:** §4.2 (reflection + bounded retries), §8 (the six rubric dimensions).
- **Files involved:**
  - `creek-tools/creek/author/reflection.py` — judge with the six checks.
  - `creek-tools/creek/author/conductor.py` — retry loop + escalation.
  - reuse FEAT-040.x voice-fidelity lint; provenance check; privacy-tier check.
  - `creek-tools/tests/` — mutation-grade reflection tests.
- **Prior decisions:** Six checks — voice fidelity, ontological accuracy, citation completeness, privacy compliance, paradox preservation, attribution correctness. Citation + privacy are HARD gates for research. `max_author_rounds` bounds [1,10]. ESCALATE surfaces a human-in-the-loop signal; never ship on exhaustion.
- **State of the world:** Reflection is a pass-through stub returning PASS.

## Output Format

A single PR containing:

- [ ] Real Reflection node returning `PASS | REVISE | ESCALATE` + structured findings.
- [ ] Conductor retry loop honoring `max_author_rounds`; ESCALATE on exhaustion.
- [ ] Mutation-grade tests: each defect (uncited claim, alias misuse, privacy leak, resolved paradox, false attribution) yields the correct verdict + finding — assert the exact verdict, not merely "not PASS".

## Examples

```python
verdict = reflect(bad_draft_with_uncited_claim, contract=research, vault=fixture)
assert verdict.decision == "REVISE"
assert any(f.dimension == "citation_completeness" for f in verdict.findings)

# Exhausted retries escalate rather than ship:
result = run_author(medium="research", query=unanswerable, vault=fixture, max_rounds=2)
assert result.verdict == "ESCALATE"
```

## Constraints

**Scope fence:** Do not implement prompt-caching/model-tiering polish (ISSUE_07) or any non-research
medium (EPIC_04). Reflection model is configurable; default to a cheap model with escalation on
borderline scores.

**Anti-bypass (verbatim, non-negotiable):**

> No `noqa`, `# type: ignore`, `pylint: disable`, `eslint-disable`, or equivalent
> linter/type-checker silencers. Fix the root cause. The only exception is the documented 4-line
> escape hatch (third-party library bug / language-version compatibility / benchmarked
> performance necessity / generated code) — and it must include the reason, a reference URL, an
> alternative considered, and a review date. See the `max-quality-no-shortcuts` skill.

**Tracer-code invariant:** The desk ships only PASS drafts; a failing draft loops then escalates —
the demoable pipeline now has its quality gate.

## Definition of Done (stay-green)

- [ ] All new and existing tests pass (`cd creek-tools && ./scripts/test.sh --all`).
- [ ] `pre-commit run --all-files -c creek-tools/.pre-commit-config.yaml` is clean.
- [ ] Coverage on changed lines ≥90% branch; docstrings ≥95%; complexity ≤10; MyPy strict.
- [ ] Each rubric dimension has a mutation-grade test asserting the exact verdict.
- [ ] PR body includes `Refs #EPIC_02_NUMBER` and `Closes #THIS_ISSUE_NUMBER`.
- [ ] Latest Claude reviewer `Verdict:` on HEAD is `LGTM`.

## Labels

`spec-decomposition`, `edges`, `author-desk`
