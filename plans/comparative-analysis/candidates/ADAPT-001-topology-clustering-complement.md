# ADAPT-001: Topology Clustering as Complement to Embeddings

**Verdict:** ADAPT
**Source system:** Graphify
**Affects:** Creek Vault data layer
**Roadmap target:** v0.2 (after the compiled layer is committed)
**Estimated complexity:** M
**Conflicts with non-negotiables?** none

## What it is

Graphify clusters its NetworkX graph using Leiden community detection ([README v4](https://github.com/safishamsi/graphify/blob/v4/README.md), [`how-it-works.md` v6](https://github.com/safishamsi/graphify/blob/v6/docs/how-it-works.md)). Communities are derived from edge density rather than vector similarity. The resulting "Community Hubs" section of the audit report names logical components in the corpus that may or may not align with what an embedding-only view would surface.

## Why it's interesting

Creek's eddies today are produced by density-based clustering on the resonance graph (already topology-flavored) but the user has explicitly named that a *Leiden* layer over the same graph might surface a different and complementary view. Embedding-cosine similarity finds fragments that are semantically close in vector space; Leiden communities find fragments that are densely interconnected in the resonance graph regardless of pairwise vector distance. These are different cuts of the same data. A fragment can be a Leiden community-bridge (high betweenness centrality, few high-cosine-similarity neighbors) — exactly the kind of liminal-cross-eddy material that `creek mine` is supposed to surface.

The user's framing is correct: topology *complements* embeddings; it doesn't replace them. Embeddings stay.

## Fit with Creek Vault and/or CrawDad

Creek already has the substrate. `creek link --method eddies` produces clusters via density-based clustering. Adding a Leiden pass over the same resonance graph is additive: the same edges, a different community-detection algorithm, a different output set.

Three places the Leiden view becomes useful:

1. **Audit report (ADOPT-005) "Community Hubs" section.** Top N Leiden communities sized by fragment count, with a one-line summary per community. Treats topology and embeddings as parallel views of vault structure.
2. **Mining strategy: `community-bridge`.** Find fragments with high betweenness centrality across Leiden communities — the boundary objects between otherwise-disjoint topical clusters. Adds a fifth strategy alongside the existing four (liminal-cross-eddy, thread-terminus, resonance-chain, wavelength-phase).
3. **Eddy formation.** When density-based clustering and Leiden disagree on what an eddy is, that disagreement is itself information — it might mean an eddy is fragmenting or two eddies are merging. Surface this as a lint check.

## Translation if adapted

Two important Creek-flavored adaptations:

1. **Don't drop density-based clustering for Leiden.** Run both. The eddies the user already has are produced by an algorithm tuned to Creek's needs; Leiden is an additional view, not a replacement. Configurable choice in `LinkingConfig`.
2. **Wavelength-phase as a Leiden constraint.** The vanilla Leiden clusters by edge density alone. A Creek-flavored Leiden could incorporate phase as a node attribute and prefer communities that cohere in phase as well as in topology. This is a research direction, not a v0.2 deliverable, but worth flagging.

Implementation: `graspologic` is what Graphify uses; `python-igraph` and `networkx` both have Leiden bindings. `sentence-transformers` and `scikit-learn` are already Creek dependencies; adding one of these for Leiden is small.

## Dependencies

- Depends on: ADOPT-005 (audit report has the section that surfaces Leiden output).
- Pairs with: ADAPT-002 (the four-worker decomposition — Surveyor is the natural home for relationship-discovery work, including Leiden).

## Acceptance criteria

- A `creek link --method leiden` command exists and produces Leiden communities over the resonance graph.
- The audit report has a "Community Hubs" section that surfaces Leiden communities, with the top N sized by fragment count.
- The existing density-based eddies remain the default; Leiden is additive.
- A `community-bridge` mining strategy is implemented in `creek mine`.
- A regression test verifies that disagreements between density-based eddies and Leiden communities are surfaced (somewhere in the lint output) rather than silently overwritten.
- The user has explicitly defended embeddings as primary; this candidate's docstring includes a one-line statement that Leiden is a complement to, not a replacement for, embedding-based resonance.
