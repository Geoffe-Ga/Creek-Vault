# INC-006: Embeddings cache is `.npz` (or absent), not `embeddings.parquet`

**Severity:** Medium
**Category:** INC
**Estimated complexity:** M (≤1d)
**Parallelizable with peers in same category:** yes
**Discovered by:** Dimension 5; confirmed by parallel agent

## Files affected
- `creek/link/embeddings.py:140-147` — `generate_embeddings`/`save_embeddings`
- `creek-tools/docs/linking.md:85` — claim "embeddings are cached at `<vault>/00-Creek-Meta/embeddings.parquet`"
- `creek-tools/docs/configuration.md:239` — same claim in the "Where things live" table

## Dependencies
None.

## Blockers
None directly. But the cache claim is the main reason a user would expect re-running `creek link --method embeddings` to be cheap.

## Reproduction
```bash
grep -rn "parquet" creek-tools/creek/  # zero hits
```

## Analysis

`docs/linking.md`:
> Re-running is cheap because embeddings are cached at `<vault>/00-Creek-Meta/embeddings.parquet` and only stale rows are recomputed.

The codebase has *no* parquet handling. There is a `save_embeddings` that writes `.npz`, but it doesn't:
- Track per-fragment freshness (mtime, content hash)
- Skip recomputation for unchanged fragments
- Live at the documented path

So every `creek link` run computes embeddings from scratch — minutes-to-tens-of-minutes even for moderate vaults.

Confidence: verified.

## Proposed remediation

Implement an actual cache:
1. Schema: `(fragment_id, content_hash, model_name, embedding_vector, computed_at)`.
2. Storage: parquet via `pyarrow` (matches the docs and is suited to vector data) — or sqlite with a vector column. Parquet is fine.
3. On load: read the cache, key by `fragment_id`. For each fragment, recompute only if `content_hash` mismatches or `model_name` differs.
4. Refuse to use a cache from a different `EmbeddingsConfig.model` — version the cache by model name.
5. Add a `--rebuild` flag (also documented but missing — separate issue) to force recomputation.

If `pyarrow` is too heavy as a hard dependency, mark it optional and document the fallback (pickle/.npz with similar key+hash semantics).

## Acceptance criteria

- After running `creek link --method embeddings` twice, the second run is at least 10× faster on a 1k-fragment vault and recomputes only fragments whose content hash changed.
- The cache file lives at `<vault>/00-Creek-Meta/embeddings.parquet` (or wherever the docs are updated to say).
- Switching `embeddings.model` triggers a recompute (cache invalidated by model name).
- A `--rebuild` flag exists and forces recomputation.

## References
- `creek-tools/docs/linking.md:85`
- `creek-tools/docs/configuration.md:239`
- `creek/link/embeddings.py`
