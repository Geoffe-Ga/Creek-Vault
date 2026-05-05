# PERF-003: `SemanticDeduplicator.find_duplicates` is O(N²) pairwise

**Severity:** High
**Category:** PERF
**Estimated complexity:** M (≤1d)
**Parallelizable with peers in same category:** yes
**Discovered by:** Dimension 9; confirmed by parallel agent

## Files affected
- `creek/clean/semantic_dedup.py:268-276` (and surrounding `find_duplicates`)

## Dependencies
None.

## Blockers
None for small vaults. Will OOM or timeout for vaults of >5k fragments.

## Reproduction
On a 10k-fragment vault, `creek clean duplicates` runs 50M cosine-similarity computations. With 384-dim embeddings that's tractable in raw FLOPs (a few seconds in numpy) but the implementation iterates pairs in pure Python, so wall-clock is much worse.

```python
# semantic_dedup.py
for i in range(len(ids)):
    for j in range(i + 1, len(ids)):
        sim = cosine_similarity(embeddings[i], embeddings[j])
        ...
```

## Analysis

A double for-loop in Python over 10k IDs ≈ 10⁸ iterations. Even without the cosine call, the Python overhead alone is minutes. With the cosine call (numpy or manual), much worse.

This shows up as soon as the vault gets non-trivial. Combined with PERF-006 (voice exemplar load-everything-into-memory), the dedup pass is the second-largest hot spot.

Confidence: verified.

## Proposed remediation

Use a vectorised + index-backed approach:
- Compute the full N×N cosine matrix once with `embeddings @ embeddings.T` (after L2 normalisation). Memory: N² × 4 bytes. At N=10k that's ~400MB — borderline but doable.
- For larger N, swap in a spatial index (`sklearn.neighbors.NearestNeighbors` with metric="cosine", or `faiss.IndexFlatIP` after normalisation). Both are sub-quadratic for the typical "I want all pairs above threshold" use case.

For the first remediation, just do the matmul. For the second, gate behind a config flag (`deduplication.use_faiss: bool`). FAISS is a heavyweight optional dep.

## Acceptance criteria

- 10k-fragment dedup completes in <30 seconds (down from minutes-to-hours).
- Memory stays under 500MB for N ≤ 10k.
- Same set of duplicate pairs is reported (regression test on a synthetic dataset).
- Clear documentation of when to enable the FAISS path.

## References
- `creek/clean/semantic_dedup.py`
- FAISS docs: <https://github.com/facebookresearch/faiss>
