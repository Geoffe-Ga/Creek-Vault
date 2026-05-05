# REJECT-001: "No Embeddings, No Vector DB"

**Verdict:** REJECT
**Source system:** Graphify (and partly Karpathy's "no vector DB at personal scale")
**Affects:** Creek Vault data layer
**Roadmap target:** N/A
**Estimated complexity:** N/A
**Conflicts with non-negotiables?** wavelength (indirectly), voice (indirectly)

## What it is

Graphify markets a "topology-based, no embeddings, no vector DB" position ([README v4](https://github.com/safishamsi/graphify/blob/v4/README.md)). Similarity recall comes from LLM-emitted `semantically_similar_to` edges + Leiden community detection, not from vector cosine similarity. Karpathy's "no vector DB at personal-knowledge-base scale" is a softer related claim — at ~100 articles, an `index.md` + on-demand page reads beats a vector index on cost and reliability.

## Why it's interesting

Both claims are well-motivated for their respective use cases. Graphify is operating on code where call-graph and import-graph topology already encode much of "what's related to what." Karpathy is operating at a corpus size where in-context navigation works.

Neither claim generalizes to Creek Vault.

## Fit with Creek Vault and/or CrawDad

It doesn't. **The user has explicitly stated that embeddings stay**, and the reasoning is correct:

1. Wavelength-phase resonance often connects fragments with no surface vocabulary overlap. A Discord message about feeling burnt out and a journal entry about a project withering both encode Withdrawal-phase content, but neither uses the word "withdrawal." Embedding cosine similarity catches this; LLM-emitted `semantically_similar_to` edges from a one-shot extraction pass would have to *explicitly notice* it during compile, which is exactly the failure mode Graphify's privacy/recall trade-off accepts.
2. Cross-source synchronicity detection (cosine > 0.9, > 30 days apart, different platforms) is a Creek-specific feature that depends on robust vector similarity. A topology view doesn't surface this — there's no edge between fragments that have never been linked, and synchronicities are exactly the unlinked pairs.
3. The voice-skill tree depends on finding fragments that *resonate* with a target voice exemplar across thousands of fragments. Embedding similarity is the right tool; topology can't traverse what isn't already linked.
4. Even Karpathy's "no vector DB at personal-knowledge-base scale" applies only to the *query path* — at ~100 wiki pages, an `index.md` summary fits in context. Creek's vault has tens of thousands of fragments at the bottom layer; the compiled layer can be context-window-sized (ADOPT-007), but the resonance discovery underneath the compiled layer cannot be.

## Reasoning if rejected or deferred

The reject is on the *replacement* claim. Topology *complements* embeddings (see ADAPT-001 for Leiden as a complementary view); the compiled-layer query path can route through `index.md` rather than a vector index (see ADOPT-007). Both are good adopts. What's rejected is the framing that embedding-based resonance is unnecessary or replaceable.

This verdict could only flip if:

- The compiled layer scaled to a size where resonance discovery between fragments became unnecessary because the compiled pages absorbed the function. (Unlikely — compiled pages are summaries, not searchable corpus.)
- A compelling LLM-emitted-edge model demonstrated wavelength-phase recall as good as embedding cosine on a held-out test set. (Open research; not a v1 dependency.)

## Dependencies

- Adjacent to (not blocking): ADAPT-001 (topology as complement), ADOPT-007 (`index.md` as query path for compiled layer).

## Acceptance criteria

N/A — this is a rejection, recorded so the question doesn't get re-litigated. The acceptance criterion is documentary: when the next person reads this and asks "should we drop embeddings to chase Graphify's privacy story?" the answer is in this file with reasoning.
