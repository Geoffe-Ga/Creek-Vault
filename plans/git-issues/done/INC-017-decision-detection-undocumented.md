# INC-017: Decision-support layer documented in spec §12 has no end-user docs

**Severity:** Medium
**Category:** INC
**Estimated complexity:** M (≤1d)
**Parallelizable with peers in same category:** yes
**Discovered by:** Dimension 5

## Files affected
- `creek/generate/decisions.py` — implementation exists
- `creek-tools/docs/generation.md` — does not document `creek report --type decision`
- `creek-tools/docs/` overall — no decision-pipeline doc

## Dependencies
None.

## Reproduction
- `00-Creek-Meta/Ontology/creek_ontology_agent_prompt.md` §12 details the decision lifecycle (sensing → deliberating → committing → enacting → reflecting), decision detection criteria, decision-context gathering, and the anti-manipulation guardrail.
- `creek/generate/decisions.py` exists.
- `docs/generation.md`'s `--type` table includes `decision`, but no `creek-tools/docs/decisions.md` exists.
- The CLI's `report --type decision` route is not user-documented.

## Analysis

A whole feature area — automatic decision detection from fragment content, decision-note generation, decision-context aggregation — exists in code but is invisible to a reading user. The spec §12 also includes specific behaviours that need verification:
- Decision keyword detection ("should I", "trying to decide", etc.)
- Reopening prevention
- "Wavelength phase at opening" recording
- The anti-manipulation list (§12.4) — does the implementation honour it?

Until these are documented and the implementation verified to match, decisions ship as a black box.

## Proposed remediation

Add `creek-tools/docs/decisions.md` covering:
- The decision lifecycle and its frontmatter shape (matches `creek/models.py:Decision`)
- How `creek report --type decision` works
- The anti-manipulation guardrail (§12.4) and how the implementation enforces it
- Re-opening behaviour (and any state-transition validation — see BUG candidates around state)

Cross-reference from `docs/generation.md` and from `docs/README.md`.

## Acceptance criteria

- The new doc exists and matches the implementation.
- A user can produce a decision note from a fragment by running a documented command.
- The anti-manipulation guardrails are testable and tested.

## References
- `00-Creek-Meta/Ontology/creek_ontology_agent_prompt.md` §12
- `creek/generate/decisions.py`
- `creek/models.py:Decision`, `DecisionStatus`, `DecisionCandidate`
