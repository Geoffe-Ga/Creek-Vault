# ADR-0004: Embeddings stay local-only; hosted embedding backends deferred

- **Status**: Accepted
- **Date**: 2026-06-10
- **Driving issue**: #624 (follow-up to epic #603, swappable LLM providers).

## Context

Epic #603 made the **completion** backend selectable (Anthropic / OpenAI /
Gemini / Ollama) with keys from env and a cloud-egress consent gate. Embeddings
were deliberately untouched: the resonance/linking path is hardwired to a local
sentence-transformers model (`EmbeddingsConfig.model = "all-MiniLM-L6-v2"`,
384 dimensions), cached on disk at `00-Creek-Meta/embeddings.parquet` inside
the user's vault.

Someone who sets `llm.provider: openai` might reasonably expect embeddings to
follow. The question #624 poses: is hosted-embedding support (OpenAI
`text-embedding-3-*`, Gemini `text-embedding-004`, …) wanted, or is local-only
intentional?

Facts that drive the decision:

1. **Embeddings touch the entire vault, not a prompt at a time.** Completion
   calls send one fragment's worth of content per request, gated by explicit
   consent. Embedding ships *every fragment's full text* off-device on first
   index and re-ships it on every re-embed. It is the largest possible privacy
   egress surface in the pipeline, against the project's local-first posture.
2. **Provider choice is index-defining, not call-defining.** A completion
   provider can change per run with no migration. An embedding model defines
   the vector space: switching models (or providers) changes dimensionality
   (384 for MiniLM vs. 1536/3072 for `text-embedding-3-*` vs. 768 for
   `text-embedding-004`) and invalidates `embeddings.parquet` wholesale —
   every fragment must be re-embedded and every similarity threshold
   re-calibrated (`embeddings.similarity_threshold` is tuned for MiniLM's
   cosine distribution). Cross-model vectors must never be compared; a
   half-migrated cache silently produces garbage resonances.
3. **The known scaling pain is not embedding quality.** The O(n²) pairwise
   linking pass is what fails on large vaults (#596 capped it); hosted
   embeddings would not address that and would add per-token cost and latency
   to a path that is currently free, offline, and private.
4. **Recurring cost asymmetry.** Completions are occasional and user-triggered;
   embedding is bulk and re-runs across the whole corpus. A 10k-fragment vault
   re-embeds everything on any model change — hosted pricing turns a free
   local operation into a metered one with little demonstrated quality need.

## Decision

**Embeddings remain local-only (sentence-transformers) by design.** No
`EmbeddingsProvider` abstraction, factory, optional cloud SDK deps, or env-key
discovery is introduced for embeddings at this time. `EmbeddingsConfig.model`
continues to select among *local* sentence-transformers models only.

Local-only is recorded here as a deliberate architectural property — privacy
(vault text never leaves the device for linking), zero marginal cost, and
offline operation — not as an unfinished half of #603.

### Reopening criteria

Supersede this ADR with a hosted-embeddings design when **any** of the
following becomes true:

- A measured resonance-quality gap on real vault content is attributed to the
  local model (not to thresholds or the linking algorithm), with an evaluation
  set demonstrating a hosted model closes it.
- A vault-scale semantic feature is planned that local models demonstrably
  cannot serve (e.g. cross-lingual resonance over a multilingual vault).
- The linking path is re-architected (post-#596) such that embedding quality —
  rather than pairwise comparison cost — becomes the binding constraint.

Any successor design must include: the same cloud-egress consent gate as
completions (`creek.classify.llm.consent` — bulk embedding is strictly more
egress than a single completion), keys from env only, an explicit
index-migration story (dimensionality change ⇒ full re-embed + threshold
re-calibration + parquet invalidation), and local sentence-transformers
remaining the default.

## Consequences

- **Positive**: vault content never leaves the device for linking; the
  resonance path keeps working offline and free; no index-migration machinery
  to build or maintain; `provider:`-swap expectations are documented instead
  of silently divergent.
- **Negative**: users wanting hosted embedding quality have no supported knob;
  `llm.provider` and embeddings behavior are asymmetric (completion is
  swappable, embeddings are not) — this ADR plus the README note are the
  mitigation for that surprise.
- **Neutral**: `EmbeddingsConfig.model` still allows choosing a different
  *local* sentence-transformers model; doing so has the same re-embed
  consequence documented above (the parquet cache must be deleted to force a
  full re-index).
