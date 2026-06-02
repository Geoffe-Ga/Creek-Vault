---
medium: research-piece
structure:
  - thesis
  - evidence
  - analysis
  - citations
specialist_weights:
  retrieval: 0.35
  ontology: 0.3
  graph: 0.2
  voice: 0.15
citation_norm: every-claim
default_privacy_tier: open
reflection_rubric:
  citation_completeness: 0.35
  ontological_accuracy: 0.25
  attribution_correctness: 0.15
  voice_fidelity: 0.1
  privacy_compliance: 0.1
  paradox_preservation: 0.05
---

# research-piece.MEDIUM.md

**Medium:** `research-piece`
**Budget:** ≤1500 tokens
**Loaded by:** the Creek Writing Desk Conductor (FEAT-041)

## What the `research-piece` medium is for

An externally-facing, analytical piece — the kind of grounded long-read that
goes out under the owner's name to readers who do not share the vault. Where
`essay` *thinks* in the owner's voice and `research` answers a question
wiki-style, a research-piece *argues a thesis from evidence* and holds itself to
a stricter citation bar than either. It may lean on `11-Other-Authors/` source
material, but only **with explicit attribution** to the borrowed author.

## Structure

Compose in this order: **thesis → evidence → analysis → citations.** State the
thesis plainly, marshal grounded evidence (the owner's fragments and any
attributed other-author material), analyse what it means, then list the sources
every claim rests on.

## Specialist weighting

Retrieval is weighted highest (0.35) — a research-piece lives on the evidence it
can surface, including attributed other-author fragments. Ontology (0.3) and
Graph (0.2) keep the canonical taxonomy and neighbouring concepts correct. Voice
(0.15) is present but subordinate: the register is analytical and externally
legible, not a personal-voice performance.

## Citation norm — `every-claim`

Every substantive claim must cite its source: the ontology spec under
`00-Creek-Meta/Ontology/`, an index page, specific `source_fragments`, or — for
a borrowed idea — its `11-Other-Authors/<slug>/` origin credited by name. An
uncited claim, or a borrowed claim presented as the owner's own, is a defect.

## Reflection rubric (§8)

The reflection node weights **citation completeness** highest (0.35) — this is
the cardinal failure mode for an externally-facing piece. **Ontological
accuracy** (0.25) follows. **Attribution correctness** is weighted unusually
high here (0.15): any idea drawn from `11-Other-Authors/` must be credited to
its source, and the owner's voice must never claim a borrowed,
`reference`-weighted idea as its own. Voice fidelity (0.1) and privacy
compliance (0.1) round out the body; **paradox preservation** (0.05) keeps
contradictions surfaced from `10-Liminal/` intact rather than tidied away. An
uncited or mis-attributed claim → `REVISE`; exhausting the round budget without
a `PASS` → `ESCALATE`.
