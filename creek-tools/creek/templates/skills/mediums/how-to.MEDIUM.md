---
medium: how-to
structure:
  - goal
  - prerequisites
  - steps
  - example
  - verification
  - pitfalls
specialist_weights:
  graph: 0.35
  ontology: 0.3
  retrieval: 0.2
  voice: 0.15
citation_norm: light
default_privacy_tier: open
reflection_rubric:
  runnability: 0.35
  ontological_accuracy: 0.25
  citation_completeness: 0.15
  voice_fidelity: 0.1
  privacy_compliance: 0.1
  paradox_preservation: 0.05
---

# how-to.MEDIUM.md

**Medium:** `how-to`
**Budget:** ≤1500 tokens
**Loaded by:** the Creek Writing Desk Conductor (FEAT-041)

## What the `how-to` medium is for

An externally-shareable **agentic-coding guide** — the kind of structured,
example-driven walkthrough that turns the owner's Technical fragments and the
Praxis they imply into steps another agent (or person) can actually follow.
Where `research-piece` *argues a thesis from evidence* and `book-report`
synthesises a borrowed work, a how-to *instructs*: it states a concrete goal,
lists what must already be true, walks ordered steps, shows a worked example,
proves the result, and names the traps. The cardinal failure mode is not a
missing citation but a step that does not run.

## Structure

Compose in this order: **goal → prerequisites → steps → example →
verification → pitfalls.** State the goal plainly and concretely. List the
prerequisites a reader must satisfy first. Walk the steps in dependency order,
each one runnable on its own. Show a worked **example** — a real command,
snippet, or invocation, not a paraphrase. Give a **verification** the reader can
run to confirm success. Close with the **pitfalls**: the failure modes the
owner's fragments and Praxis warn against. The example and verification sections
are load-bearing: a how-to without them is a description, not a guide.

## Specialist weighting

Graph is weighted highest (0.35) and Ontology next (0.3): together they surface
the owner's **Technical** fragments and the **Praxis** linkage between them —
the connected procedure the guide is made of, not isolated facts. Retrieval
(0.2) is present to pull the concrete fragments (commands, snippets, configs)
each step rests on. Voice (0.15) is deliberately low: a how-to is legible and
imperative, not a personal-voice performance — clarity and correctness outrank
register.

## Citation norm — `light`

A how-to favours **runnable steps over dense citation**. Where
`research-piece`/`book-report` demand `every-claim`, a guide cites *lightly*:
ground the procedure in the owner's `source_fragments` and any Praxis it
realises, but do not interrupt the steps with a citation per sentence. The bar a
how-to must clear is that the steps *work*, not that every clause is footnoted —
so citation is `light` and the reflection weight shifts onto runnability.

## Reflection rubric (§8)

The reflection node weights **runnability** highest (0.35) — for a guide,
steps that do not actually run (a missing prerequisite, an out-of-order step, an
example that errors) are the cardinal failure, ahead of any citation gap.
**Ontological accuracy** (0.25) follows: the Technical/Praxis mapping the steps
lean on must be canonically correct. **Citation completeness** (0.15) keeps the
light grounding honest without demanding a footnote per line. Voice fidelity
(0.1) and privacy compliance (0.1) round out the body; **paradox preservation**
(0.05) keeps any surfaced "it depends" caveat intact rather than flattened into
false certainty. A step that cannot be run as written → `REVISE`; exhausting the
round budget without a `PASS` → `ESCALATE`.
