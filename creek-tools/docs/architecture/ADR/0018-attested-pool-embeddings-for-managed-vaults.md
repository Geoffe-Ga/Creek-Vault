# ADR-0018: Managed vaults without a writer's key ground through the attested pool's embedding model

- **Status**: Accepted (owner decisions: ADR-0016 Q2 = option (i), 2026-10-20,
  comment on #1849; release decision R4-4, 2026-10-27)
- **Date**: 2026-10-31
- **Decider**: Geoff Gallinger, repository owner. R4-4 is copied into
  adepthood in
  [`prompts/claude-comms/2026-10-27-release-decisions.md`](https://github.com/Geoffe-Ga/adepthood/blob/main/prompts/claude-comms/2026-10-27-release-decisions.md):
  "Wire Privatemode's embedding model (`qwen3-embedding-4b`) for users who do
  **not** bring their own key." An agent wrote this record down; it decided
  nothing.
- **Driving issue**: #1904 (ADR-0016 build sub-issue 7 of 7, Decision 1
  item 13).
- **Amends**: [ADR-0004](0004-embeddings-stay-local.md), **for managed vaults
  only**. ADR-0004's text is unchanged and it stays in force everywhere else.
  This record supplies the managed-vault exception its Decision forbids and
  waives, for the pool only, the successor clause its Reopening criteria set.

## Context

ADR-0004 keeps embeddings local (sentence-transformers) by design. A managed
vault cannot honour that: its image installs no `embeddings` extra and a
1 GB Machine cannot hold a model, so the grounding session cannot be built
and every managed-vault reflection is ungrounded today (ADR-0016 Context).
The owner's reason for changing that is recorded on #1849: grounded
reflections and letters are central to the value proposition, so ungrounded
is not an acceptable steady state.

ADR-0016 Decision 1 item 13 set out three options. (ii), baking the local
model into the image, fails R1 and R2 and is reference only. (iii) is staying
ungrounded. The owner chose (i): the shared pool serves the embedding model
too — under O5 only with an attested embedding model inside the measurement
set, and otherwise (iii). R4-4 named the model: Privatemode's
`qwen3-embedding-4b`, for writers who do not bring their own key.

None of ADR-0004's three reopening criteria is met — there is no measured
quality gap, no feature local models cannot serve, and no re-architecture in
which quality binds. The argument is cost and capacity on a managed Machine,
which is exactly why this is an owner decision and not one a lane could make.

## Decision

1. **The route.** On a managed vault (`CREEK_MANAGED_VAULT` present), a
   request whose generation stage resolves to the pool and that carries no
   writer's key that will answer it is grounded by the pool's embedding
   model: the query plus at most 256 live embeds of cache misses at
   reflection time (`creek_mcp/tools/reflect.py`), and the bulk pass behind
   `POST /v1/links` (`creek/link/link_engine.py`). Embedding never runs at
   ingest, and `/v1/classifications` does not embed. The decision is made in
   one place, `creek_mcp/grounding_route.py`, from configuration alone.
2. **Under every item of ADR-0016 Decision 1.** The dial goes through
   `creek/classify/llm/pool_boundary.py` and nowhere else (T2), to the one
   pinned destination, over the stdlib transport that reads no proxy
   variable, with no retries. Each fragment's **own** tier is handed to the
   router's chokepoint predicate (`refuses_off_device`) before any request
   is built; INTIMATE and untiered content (`None`, or a value naming no
   tier, per #2016) is refused inside the vault, and every batch declares the
   one wire tier all its texts share (`unclassified` declares `personal`). A
   batch carries at most 256 texts and at most 1 MiB, and few enough that its
   worst-case answer fits the 8 MiB embeddings response cap. The body is
   exactly `model`, `input`, `tier` and `deadline_ms`: the texts to embed.
   The vault's `00-Creek-Meta/embeddings.parquet` never leaves the vault and
   nothing is cached across vaults.
3. **Identity, pinned.** The operator's model-package manifest gains one
   optional field, `embedding`: the vendor's embedding model id, its
   **dimensionality**, the **similarity threshold** calibrated for it, and a
   `runtime_inventory_digest` binding that model to the manifest's runtime
   exactly as the generation pin does. The pool's identity listing must
   report that identity before any text is sent (T1), and an answer naming
   another model or of another dimensionality is discarded whole. The field
   is additive: every existing manifest loads unchanged, with no embedding
   pin. ADR-0016 R8 said the manifest schema is unchanged; this is the one
   addition, and it is optional.
4. **The vector space is recorded in the parquet.** Every row records its
   route, model, dimensionality and threshold. A linker reads only rows from
   its own space, so a model, route, dimensionality or threshold change is a
   detectable mismatch that re-embeds, never a silent comparison of vectors
   from two spaces. A parquet written before this record reads as the local
   route, which preserves every self-hosted vault's cache.
5. **Fallback is (iii), never elsewhere.** If no pool destination is pinned,
   the manifest cannot be loaded, or it pins no embedding model, a
   pool-routed reflection is answered **ungrounded**, with nothing embedded
   and nothing sent. If the identity listing disagrees, a batch is refused,
   or an answer is unusable, the pass stops at that point with no retry. No
   embedding ever leaves by an unattested route to make grounding work.
6. **What does not change.** A self-hosted vault keeps local
   sentence-transformers as the default and keeps ADR-0004's consent gate. A
   request carrying a writer's key keeps its route (ADR-0015; ADR-0016 R9 and
   T10): the pool is never dialled for it, grounding included. A reflection
   routed INTIMATE is grounded, if at all, where it is answered — locally.
7. **The successor clause is waived, for the pool only.** ADR-0004 requires
   any hosted-embeddings successor to carry the completions consent gate. A
   managed vault pins that gate off (`managed_local_only`, #1864), so the
   clause could never be met there. It is waived explicitly for the attested
   pool on managed vaults; the clause's other terms are met as follows: no
   key is read from vault config (the destination is a Machine-environment
   fact, and the vendor key is the pool's, not the vault's), the
   index-migration story is item 4 and the Consequences below, and local
   sentence-transformers remains the default.

## Consequences

- **Positive**: managed-vault reflections can be grounded, which the owner
  named central to the product. On a warm vault the per-request cost is one
  short vector in one batch. The vector space is a recorded, checked fact
  rather than an assumption.
- **The vector-space migration (MiniLM to `qwen3-embedding-4b`).** Under O5
  the pool's model differs from the local default, so a managed vault's
  parquet is **vendor-specific**. A vault switching spaces — a managed vault
  first grounded by the pool, a vendor model change, an export from a managed
  vault to a self-hosted one, or the reverse — **re-embeds its corpus** and
  never compares vectors across spaces: the old rows are simply not read.
  The re-embed is **bounded** (256 texts and 1 MiB per batch, the pool's
  12 s per dial, no retries, and the per-activation vendor key cap of
  ADR-0016 Decision 3.4 as the cost brake) and **resumable** (every batch
  the pool answered is persisted when the pass that received it ends, even
  if a later batch failed, and the next pass embeds only fragments whose
  content hash has no fresh row). An
  export to a self-hosted vault carries the parquet, but that vault's local
  linker reads none of its rows and re-embeds locally.
- **INTIMATE fragments have no pool vector.** They are refused per item, so
  on a managed vault they are absent from pool-grounded retrieval and from
  the bulk pass's resonances. That is Decision 5 working as intended, not a
  gap.
- **The image still lacks a parquet reader.** The managed image installs no
  `embeddings` extra, so it has neither `pyarrow` nor `numpy`. Reflection
  grounding does not need them — the pool route embeds live within the
  256-embed bound, and ranking falls back to pure-Python cosine — but the
  bulk pass cannot persist its parquet until the image carries `pyarrow`
  (and `numpy` for the resonance scan). Adding them is a dependency and
  lockfile change the owner must approve; until then the bulk pass on a
  managed Machine fails as it does today, having sent no text it could not
  keep.
- **The dimensionality is an operator pin, not a code constant.** No
  in-repo source states the dimensionality Privatemode serves
  `qwen3-embedding-4b` at, so it is pinned in the manifest the operator
  verifies against the vendor, and every answer is checked against it.
- **Negative**: titles of OPEN, PERSONAL and UNCLASSIFIED fragments leave the
  vault, inside the attested channel, as ciphertext. The reflection receipt
  publishes no separate grounding field; `inference_source` names the one
  destination both the embeddings and the answer went to, because pool
  embeddings are only ever spent on a request the pool answers. A distinct
  grounding field would be a contract minor and is left to the owner.
- **Neutral**: ADR-0004 is otherwise unchanged; `EmbeddingsConfig.model`
  still selects among local models on a self-hosted vault.
