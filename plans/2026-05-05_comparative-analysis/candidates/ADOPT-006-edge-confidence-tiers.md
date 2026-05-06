# ADOPT-006: Edge Confidence Tiers (`computed` / `inferred` / `manual` / `ambiguous`)

**Verdict:** ADOPT
**Source system:** Graphify (with Creek-flavored adaptation of the tier names)
**Affects:** Creek Vault data layer
**Roadmap target:** v1.1 (refinement after v1.0's compiled layer is in place)
**Estimated complexity:** S
**Conflicts with non-negotiables?** none

## What it is

Graphify tags every edge in the graph with a confidence tier:

- `EXTRACTED` (1.0) — directly observed in source/AST. Deterministic.
- `INFERRED` (0.55–0.95, with explicit `confidence_score`) — LLM-emitted with self-reported probability.
- `AMBIGUOUS` — flagged for human review.

Cited from [README v4](https://github.com/safishamsi/graphify/blob/v4/README.md) (verified at time of writing). Applied uniformly across edge types.

## Why it's interesting

Creek's resonance edges today carry a `score` (cosine similarity 0–1) but no provenance about *how* the edge was produced. There's no way to distinguish:

- A resonance edge from embedding cosine similarity (deterministic numerical comparison given a model + threshold)
- A resonance edge from temporal proximity + thematic overlap (heuristic)
- A resonance edge that an LLM proposed (rare today, but a plausible v1.1 addition)
- A resonance edge a human added manually (`method: manual` in classification has the analog)

The lossy-compression risk Karpathy's pattern surfaces (ADOPT-001) is amplified when downstream consumers can't distinguish high-confidence facts from inferred ones. Voice fidelity in particular depends on this: a draft that builds on a `computed` resonance is on solid ground; a draft that builds on an `ambiguous` resonance needs to flag the move.

## Fit with Creek Vault and/or CrawDad

Creek already has a `method` field on classifications (`rules` / `llm` / `manual`). The edge equivalent would be `confidence_tier` on resonance edges, with Creek-flavored values:

- **`computed`** — deterministic numerical result given a fixed model and threshold (e.g., cosine similarity ≥ threshold). Reproducible *given the same model and threshold*; not strictly "extracted from source structure" the way Graphify's tree-sitter edges are. Different from Graphify's `EXTRACTED` because embedding outputs shift across model versions; calling cosine results `extracted` would mislead future implementers into treating them as model-version-stable, which they aren't.
- **`inferred`** — LLM-emitted or heuristic-derived, with `confidence_score` for the LLM case.
- **`manual`** — human-added; treated as ground truth for downstream consumers but distinguished from `computed` so it can be promoted/demoted independently in any future schema migration.
- **`ambiguous`** — flagged for human review; below threshold but worth surfacing.

```yaml
links:
  resonances:
    - fragment_id: frag-9c1f3a2b8e02
      score: 0.87
      confidence_tier: computed    # NEW
      method: embeddings
      generated_at: 2026-04-28T17:35:00Z
```

Tier mapping:
- `embeddings` cosine similarity ≥ threshold → `computed`.
- `temporal` proximity → `inferred` (thematic overlap is fuzzy).
- `eddies` density clustering → `inferred` (clustering is a derived view).
- LLM-proposed semantic relation → `inferred` (with `confidence_score` = LLM self-reported probability).
- Below similarity threshold but flagged for human review → `ambiguous`.
- Human-added → `manual`.

The same scheme applies to claims on compiled pages: each claim carries the tier of the underlying edge(s) it summarizes.

For CrawDad, this becomes a hedging discipline. When CrawDad surfaces a connection in conversation, it can say "you wrote about X and Y in similar terms" (computed/manual, high confidence) vs. "these *might* be the same thread, you've never said so explicitly" (inferred or ambiguous, surfaceable but flagged).

## Translation if adapted

Three Creek-flavored adaptations against Graphify's scheme:

1. **Don't use uppercase enum values in YAML.** Creek-flavored: `computed | inferred | manual | ambiguous` (lowercase, consistent with the existing `method` style).
2. **Rename Graphify's `EXTRACTED` to `computed` for embedding-derived edges.** Embeddings aren't deterministic across `sentence-transformers` model versions; "extracted" connotes "directly observed in source structure," which is true for Graphify's tree-sitter pass but misleading for cosine similarity. `computed` carries the right connotation: "deterministic given the configuration, but model-version dependent."
3. **Add a `manual` tier** distinct from `computed`. Graphify has no equivalent because Graphify is non-interactive; Creek has human-added resonances and they should be distinguishable from automated ones for migration / audit purposes.

For *paradoxes* specifically, the tier model needs a fourth value or a different field — paradoxes aren't inferred connections, they're detected contradictions. Probably better to leave the paradox layer alone and only apply confidence tiers to resonances and to claims on compiled pages.

## Dependencies

- Depends on: ADOPT-001 (compiled layer needs claim-level provenance for tiers to mean anything downstream).
- Pairs with: ADOPT-005 (audit report's "surprising connections" section ranks by tier × score).

## Acceptance criteria

- Resonance edge frontmatter carries a `confidence_tier` field with one of `computed | inferred | manual | ambiguous`.
- Each linker (`embeddings`, `temporal`, `eddies`) emits the right tier per the mapping above.
- Human-added resonances default to `manual`, not `computed`.
- Compiled-layer pages (Threads, Eddies, Frequency-index notes) carry per-claim tiers when claims are sourced from edges.
- `creek draft` can be configured to refuse drafts built primarily on `inferred` or `ambiguous` claims, or at minimum to surface the tier mix in the draft's frontmatter.
- A regression test verifies the tier mapping on each linker.
- A regression test verifies that re-embedding with a different `sentence-transformers` model produces different `score` values (proving the `computed` naming is justified vs. an `extracted` naming that would imply model-stability).
