## Role

You are a senior Python engineer working in `creek/author/conductor.py` and `creek/author/voice.py`,
fluent in the FEAT-040 voice-skill tree.

## Goal

Make the Conductor's **synthesis** real (merge the three specialists' evidence into a structured
draft where every substantive claim maps to `source_fragments`), and make the **Voice agent** real
(render the synthesized draft in the vault owner's voice using the voice-skill stack, medium- and
phase-aware).

## Context

- **Parent epic:** #EPIC_02_NUMBER
- **Predecessor issue(s):** #EPIC_02_ISSUE_04_NUMBER (all three specialists are real).
- **SPEC section:** §4.2 (synthesis + Voice agent), §5 (research contract structure).
- **Files involved:**
  - `creek-tools/creek/author/conductor.py` — evidence merge → grounded draft.
  - `creek-tools/creek/author/voice.py` — load `<vault>/creek-skills/` voice stack and render.
  - `creek-tools/tests/` — synthesis + voice tests.
- **Prior decisions:** Synthesis stays grounded — no claim without provenance. Voice rendering reuses the existing voice stack; it does NOT pull from `11-Other-Authors/` for voice (only ideas, already attributed). For research medium, voicing is light (clarity over flourish per the contract).
- **State of the world:** Conductor synthesis + Voice are still stubs; the three specialists now return real evidence.

## Output Format

A single PR containing:

- [ ] Real synthesis: structured draft with per-claim provenance, honoring the research contract structure.
- [ ] Real Voice agent rendering via the voice-skill tree.
- [ ] Tests: every claim in the draft carries provenance to a real fragment ID; voice rendering applies on the fixture; a claim lacking evidence is dropped or flagged, never fabricated.

## Examples

```python
draft = run_author(medium="research", query="What is F6 Pluralism?", vault=fixture)
assert all(claim.source_fragments for claim in draft.claims)   # grounded
assert draft.rendered_text                                     # voiced
```

## Constraints

**Scope fence:** Do not implement the Reflection node / retries (ISSUE_06) or prompt-caching polish
(ISSUE_07). Voice must not train on or quote `11-Other-Authors/` as the owner's own words.

**Anti-bypass (verbatim, non-negotiable):**

> No `noqa`, `# type: ignore`, `pylint: disable`, `eslint-disable`, or equivalent
> linter/type-checker silencers. Fix the root cause. The only exception is the documented 4-line
> escape hatch (third-party library bug / language-version compatibility / benchmarked
> performance necessity / generated code) — and it must include the reason, a reference URL, an
> alternative considered, and a review date. See the `max-quality-no-shortcuts` skill.

**Tracer-code invariant:** The desk now produces a real, cited, voiced draft end-to-end on the
fixture (Reflection still a pass-through stub) without breaking any surface.

## Definition of Done (stay-green)

- [ ] All new and existing tests pass (`cd creek-tools && ./scripts/test.sh --all`).
- [ ] `pre-commit run --all-files -c creek-tools/.pre-commit-config.yaml` is clean.
- [ ] Coverage on changed lines ≥90% branch; docstrings ≥95%; complexity ≤10; MyPy strict.
- [ ] Grounding proven: no claim without provenance.
- [ ] PR body includes `Refs #EPIC_02_NUMBER` and `Closes #THIS_ISSUE_NUMBER`.
- [ ] Latest Claude reviewer `Verdict:` on HEAD is `LGTM`.

## Labels

`spec-decomposition`, `core`, `author-desk`
