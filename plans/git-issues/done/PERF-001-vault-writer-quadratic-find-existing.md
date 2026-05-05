# PERF-001: `VaultWriter._find_existing` is O(N²) per write

**Severity:** High
**Category:** PERF
**Estimated complexity:** M (≤1d)
**Parallelizable with peers in same category:** yes
**Discovered by:** Dimension 9

## Files affected
- `creek/vault/writer.py:291-310`

## Dependencies
Closely related to BUG-006 (race) and PERF-002 (provenance log). Best fixed together.

## Blockers
None for small vaults; severe for vaults of 5k+ fragments.

## Reproduction
Time `VaultWriter.write_fragment` for a fragment about to land in `01-Fragments/Conversations/`. As that subfolder grows, write time grows linearly per write. Writing N fragments to one folder is O(N²) wall time.

```python
import time
# pre-create 5000 fragments in target_dir
for i in range(100):
    t0 = time.time()
    writer.write_fragment(make_fragment(i))
    print(time.time() - t0)
# observe: write time slowly creeps up
```

## Analysis

```python
def _find_existing(self, model_id: str, target_dir: Path) -> Path | None:
    if not target_dir.exists():
        return None
    for md_file in target_dir.glob("*.md"):
        post = frontmatter.load(str(md_file))
        if post.get("id") == model_id:
            return md_file
    return None
```

For every write, the writer enumerates every `.md` in the target dir and parses each one's YAML frontmatter to find a matching ID. With N existing files, every new write is O(N). Writing N fragments → O(N²) total work *and* O(N) wall time per write near the end. For the documented "10k-fragment vault", this is hundreds of milliseconds of disk + parse work per write.

Concrete: if `frontmatter.load` takes 1ms per file and N=10000, every write spends 10s scanning. Ingesting a fresh source becomes prohibitively slow.

Confidence: verified — read writer.py.

## Proposed remediation

Maintain a per-directory index mapping `id → relative_path`, persisted as `<dir>/.id-index.json` (or sqlite). On write:
1. If the index doesn't exist, scan once and build it (O(N)).
2. Look up the new ID — O(1).
3. On hit, return existing path.
4. On miss, write the file, update the index.
5. Index entries are durable across runs.

Pair with BUG-006's atomic-write change so the index is updated transactionally with the file.

Alternative: use the OS to do the lookup. Filename pattern `{date}-{title}-{id}.md` would let `Path.glob(f"*-{model_id}.md")` find existing files in O(1) on most filesystems. Less invasive but slightly less robust.

## Acceptance criteria

- Writing 10000 fragments to one directory takes O(N), not O(N²) — measured median per-write time stays roughly flat.
- Re-running ingestion (everything already exists) takes ~constant per fragment, not linear.
- No fragment is duplicated under id.
- Existing tests for `_find_existing` semantics still pass.

## References
- `creek/vault/writer.py:291-310`
- BUG-006 (related race), PERF-002 (related provenance issue)
