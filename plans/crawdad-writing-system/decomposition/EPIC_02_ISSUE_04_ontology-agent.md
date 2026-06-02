## Role

You are a senior Python engineer working in `creek/author/agents.py`, fluent in `creek/classify/`,
`creek/generate/voice.py`, and `creek/generate/wavelength.py`.

## Goal

Replace the **Ontology** specialist stub with a real agent that supplies the "analytics" layer:
given a query and the evidence so far, it returns structured ontological analysis — which
frequencies/phases/modes the topic resonates with, dosage (medicine/toxic) framing, and voice/phase
context — all in canonical taxonomy, with provenance.

## Context

- **Parent epic:** #EPIC_02_NUMBER
- **Predecessor issue(s):** #EPIC_02_ISSUE_03_NUMBER (Graph + Retrieval feed the Ontology agent).
- **SPEC section:** §4.1 (Ontology agent row), §6 (resonance/liminal awareness), §8 (ontological accuracy gate this agent supports).
- **Files involved:**
  - `creek-tools/creek/author/agents.py` — real Ontology agent.
  - reuse `creek/classify/`, `creek/generate/voice.py`, `creek/generate/wavelength.py`.
  - `creek-tools/tests/` — ontology-agent tests.
- **Prior decisions:** Output uses canonical taxonomy only (F1–F10; rising/peaking/withdrawal/diminishing/bottoming_out/restoration; modes Inhabit/Express/Collaborate/Integrate/Absorb; medicine/toxic). No aliases (INC-019). Surface `10-Liminal/` paradoxes rather than resolving them.
- **State of the world:** The Ontology agent is still a stub; Graph + Retrieval are now real.

## Output Format

A single PR containing:

- [ ] Real Ontology agent returning structured ontological evidence with provenance.
- [ ] Reuse of existing classify/voice/wavelength modules (no reinvention).
- [ ] Tests: canonical taxonomy enforced; a fixture paradox is surfaced, not resolved.

## Examples

```python
ont = ontology_agent.analyze(query="F6 medicine vs toxic across phases", evidence=ev, vault=fixture)
assert ont.frequencies <= {f"F{i}" for i in range(1, 11)}
assert "withdrawal" in ont.phase_notes        # canonical phase name
assert ont.paradoxes                          # surfaced, not flattened
```

## Constraints

**Scope fence:** Do not perform final synthesis or voicing (ISSUE_05) or judging (ISSUE_06). No new
ML models — reuse existing analytics modules.

**Anti-bypass (verbatim, non-negotiable):**

> No `noqa`, `# type: ignore`, `pylint: disable`, `eslint-disable`, or equivalent
> linter/type-checker silencers. Fix the root cause. The only exception is the documented 4-line
> escape hatch (third-party library bug / language-version compatibility / benchmarked
> performance necessity / generated code) — and it must include the reason, a reference URL, an
> alternative considered, and a review date. See the `max-quality-no-shortcuts` skill.

**Tracer-code invariant:** The desk still runs end-to-end; Voice + Reflection remain stubs and the
pipeline stays green with three real specialists.

## Definition of Done (stay-green)

- [ ] All new and existing tests pass (`cd creek-tools && ./scripts/test.sh --all`).
- [ ] `pre-commit run --all-files -c creek-tools/.pre-commit-config.yaml` is clean.
- [ ] Coverage on changed lines ≥90% branch; docstrings ≥95%; complexity ≤10; MyPy strict.
- [ ] Canonical-taxonomy + paradox-preservation proven by test.
- [ ] PR body includes `Refs #EPIC_02_NUMBER` and `Closes #THIS_ISSUE_NUMBER`.
- [ ] Latest Claude reviewer `Verdict:` on HEAD is `LGTM`.

## Labels

`spec-decomposition`, `core`, `author-desk`
