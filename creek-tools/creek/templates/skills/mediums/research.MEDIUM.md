---
medium: research
structure:
  - thesis
  - evidence
  - synthesis
  - citations
specialist_weights:
  graph: 0.3
  ontology: 0.3
  retrieval: 0.3
  voice: 0.1
citation_norm: every-claim
default_privacy_tier: open
reflection_rubric:
  ontological_accuracy: 0.35
  citation_completeness: 0.35
  voice_fidelity: 0.1
  privacy_compliance: 0.1
  paradox_preservation: 0.05
  attribution_correctness: 0.05
---

# research.MEDIUM.md

**Medium:** `research`
**Budget:** ≤1500 tokens
**Loaded by:** the Creek Writing Desk Conductor (FEAT-041)

## What the `research` medium is for

An encyclopedic, wiki-style answer about the APTITUDE frequencies and the
Archetypal Wavelength. The reader wants an accurate, well-grounded explanation —
not a personal essay. Accuracy and citations matter more than voice flourish.

## Structure

Compose in this order: **thesis → evidence → synthesis → citations.** State the
claim plainly, marshal the grounded evidence, synthesize it into a coherent
answer, then list the sources every claim rests on.

## Specialist weighting

The Graph and Ontology specialists are weighted heavily (0.3 each) — research
answers live or die on correct ontological structure and the right neighbouring
concepts. Retrieval is weighted equally (0.3) to surface supporting fragments.
The Voice agent is weighted low (0.1): the register should be clear and
explanatory, not performative.

## Citation norm — `every-claim`

Every substantive claim must cite its source: the ontology spec under
`00-Creek-Meta/Ontology/`, a frequency/wavelength index page, or specific
`source_fragments`. An uncited claim is a defect, not a stylistic choice.

## Reflection rubric (§8)

The reflection node scores this medium with **ontological accuracy** and
**citation completeness** weighted highest (0.35 each), and **voice fidelity**
weighted low (0.1). Privacy compliance, paradox preservation, and attribution
correctness round out the rest. Misuse of the canonical taxonomy or an uncited
claim → `REVISE`; exhausting the round budget without a `PASS` → `ESCALATE`.
