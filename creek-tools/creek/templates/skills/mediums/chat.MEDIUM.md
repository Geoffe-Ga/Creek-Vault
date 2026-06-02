---
medium: chat
structure:
  - reply
specialist_weights:
  voice: 0.4
  retrieval: 0.3
  graph: 0.2
  ontology: 0.1
citation_norm: light
default_privacy_tier: open
reflection_rubric:
  voice_fidelity: 0.45
  ontological_accuracy: 0.2
  privacy_compliance: 0.15
  citation_completeness: 0.1
  paradox_preservation: 0.05
  attribution_correctness: 0.05
---

# chat.MEDIUM.md

**Medium:** `chat`
**Budget:** ≤1500 tokens
**Loaded by:** the Creek Writing Desk Conductor (FEAT-041)

## What the `chat` medium is for

A short, voiced reply — CrawDad answering a quick question in conversation. This
is the FEAT-015 conversational loop, re-homed as a medium. Lowest latency of all
the mediums; a trivial turn may skip full retrieval entirely.

## Structure

A single **reply**. No headings, no sections — just the answer, in the owner's
voice, kept under roughly `CHAT_MAX_CHARS`. Brevity is the format.

## Specialist weighting

Voice is weighted highest (0.4): a chat reply should sound like the owner, not
like an encyclopedia. Retrieval (0.3) grounds the answer when it matters; Graph
(0.2) and Ontology (0.1) keep it from drifting off the canonical taxonomy.

## Citation norm — `light`

Cite only when a claim genuinely leans on a specific fragment or the ontology.
A conversational gut-check does not need a footnote on every sentence.

## Reflection rubric (§8)

The reflection node weights **voice fidelity** highest (0.45) for chat — the
register and tone matter most here. Ontological accuracy (0.2) and privacy
compliance (0.15) follow; citation completeness is light (0.1). Register drift
or a privacy leak → `REVISE`; exhausting the round budget → `ESCALATE`.
