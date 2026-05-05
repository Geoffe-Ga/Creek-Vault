# ADOPT-006: Edge Confidence Tiers (EXTRACTED / INFERRED / AMBIGUOUS)

**Verdict:** ADOPT
**Source system:** Graphify
**Affects:** Creek Vault data layer
**Roadmap target:** v0.2 (refinement after v1's compiled layer is in place)
**Estimated complexity:** S
**Conflicts with non-negotiables?** none

## What it is

Graphify tags every edge in the graph with a confidence tier:

- `EXTRACTED` (1.0) — directly observed in source/AST. Deterministic.
- `INFERRED` (0.55–0.95, with explicit `confidence_score`) — LLM-emitted with self-reported probability.
- `AMBIGUOUS` — flagged for human review.

Cited from [README v4](https://github.com/safishamsi/graphify/blob/v4/README.md). Applied uniformly across edge types.

## Why it's interesting

Creek's resonance edges today carry a `score` (cosine similarity 0–1) but no provenance about *how* the edge was produced. There's no way to distinguish:

- A resonance edge from embedding cosine similarity (deterministic numerical comparison)
- A resonance edge from temporal proximity + thematic overlap (heuristic)
- A resonance edge that an LLM proposed (rare today, but a plausible v0.2 addition)
- A resonance edge a human added manually (`method: manual` in classification has the analog)

The lossy-compression risk Karpathy's pattern surfaces (ADOPT-001) is amplified when downstream consumers can't distinguish high-confidence facts from inferred ones. Voice fidelity in particular depends on this: a draft that builds on an EXTRACTED resonance is on solid ground; a draft that builds on an AMBIGUOUS resonance needs to flag the move.

## Fit with Creek Vault and/or CrawDad

Creek already has a `method` field on classifications (`rules` / `llm` / `manual`). The edge equivalent would be `method` on resonance edges:

```yaml
links:
  resonances:
    - fragment_id: frag-9c1f3a2b8e02
      score: 0.87
      confidence_tier: extracted    # NEW
      method: embeddings
      generated_at: 2026-04-28T17:35:00Z
```

Tier mapping:
- `embeddings` cosine similarity ≥ threshold → `extracted` (deterministic, score-based).
- `temporal` proximity → `inferred` (heuristic; thematic overlap is fuzzy).
- `eddies` density clustering → `inferred` (clustering is a derived view).
- LLM-proposed semantic relation → `inferred` (with score = LLM self-reported confidence).
- Below similarity threshold but flagged for human review → `ambiguous`.
- Human-added → `extracted` (treat human judgment as ground truth).

The same scheme applies to claims on compiled pages: each claim carries the tier of the underlying edge(s) it summarizes.

For CrawDad, this becomes a hedging discipline. When CrawDad surfaces a connection in conversation, it can say "you wrote about X and Y in similar terms" (extracted, high confidence) vs. "these *might* be the same thread, you've never said so explicitly" (inferred, surfaceable but flagged).

## Translation if adapted

Don't introduce three uppercase enum values in YAML. Creek-flavored values: `extracted | inferred | ambiguous` (lowercase, consistent with existing `method` style). The `score` field already exists for resonances; `confidence_tier` is additive.

For *paradoxes* specifically, the tier model needs a fourth value or a different field — paradoxes aren't inferred connections, they're detected contradictions. Probably better to leave the paradox layer alone and only apply confidence tiers to resonances and to claims on compiled pages.

## Dependencies

- Depends on: ADOPT-001 (compiled layer needs claim-level provenance for tiers to mean anything downstream).
- Pairs with: ADOPT-005 (audit report's "surprising connections" section ranks by tier × score).

## Acceptance criteria

- Resonance edge frontmatter carries a `confidence_tier` field with one of `extracted | inferred | ambiguous`.
- Each linker (`embeddings`, `temporal`, `eddies`) emits the right tier per the mapping above.
- Human-added resonances default to `extracted`.
- Compiled-layer pages (Threads, Eddies, Frequency-index notes) carry per-claim tiers when claims are sourced from inferred edges.
- `creek draft` can be configured to refuse drafts built primarily on `inferred` claims, or at minimum to surface the tier mix in the draft's frontmatter.
- A regression test verifies the tier mapping on each linker.
