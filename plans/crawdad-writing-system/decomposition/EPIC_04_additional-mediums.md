## Epic Summary

Extend the Creek Writing Desk to the remaining output mediums by adding medium contracts that
reuse the same agent desk: `chat`, `essay`, `research-piece`, `book-report`, and `how-to`. Each
medium is a small contract (structure + specialist weighting + citation norm + reflection rubric)
declared in the medium skill tree; no new agent code is required. Implements SPEC §5 (mediums
2–6).

## Scope

**In scope:**
- `chat` medium contract (re-homes the FEAT-015 conversational loop as a medium) — the skeleton proving the contract abstraction generalizes.
- `essay`, `research-piece`, `book-report`, `how-to` medium contracts + per-medium reflection rubrics.
- A medium-contract conformance test harness.

**Out of scope:**
- The desk internals and the `research` contract (EPIC_02).
- New retrieval agents (reuse EPIC_02's).

## Success Criteria

The epic is done when:

- [ ] Each medium has a linted contract under `00-Creek-Meta/Skills/mediums/` within the ≤1500-token budget.
- [ ] The conductor selects the right contract from a `--medium` flag and applies its rubric.
- [ ] `book-report` / `research-piece` correctly attribute `11-Other-Authors/` ideas.
- [ ] Each medium produces a passing draft on the fixture vault with medium-appropriate citation discipline.
- [ ] All child issues closed; `./scripts/check-all.sh` green on `main`.

## Child Issues

_Filled in after child issues are filed._

- [ ] #NNN — Skeleton: `chat` medium contract + conformance harness
- [ ] #NNN — Core: `essay` medium contract + rubric
- [ ] #NNN — Core: `research-piece` medium contract + rubric
- [ ] #NNN — Core: `book-report` medium contract + rubric
- [ ] #NNN — Core: `how-to` (agentic-coding) medium contract + rubric

## Sequencing Notes

- **Blocked by:** EPIC_02 (desk + `MediumContract` model + reflection node).
- **Parallel-safe:** EPIC_03.

## SPEC Reference

`plans/crawdad-writing-system/SPEC.md` — §5 (medium skill tree), §8 (per-medium reflection rubric weighting), §7.4 (attribution used by book-report/research-piece).

## Labels

`epic`, `spec-decomposition`, `mediums`
