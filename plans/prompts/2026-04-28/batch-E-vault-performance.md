# Batch E — Vault writer + dedup performance

## Role

You are a systems engineer who has profiled too many "it gets slow at scale" bugs to leave them alone. You replace O(N²) loops with index lookups, you make file writes atomic, and you treat memory growth as a correctness issue at production scale. You don't add a benchmark suite without first making the slow path fast.

## Goal

Eliminate the four worst quadratic / load-everything hot paths so a 10k-fragment vault doesn't grind: (1) `VaultWriter._find_existing` linear scan + frontmatter parse per write, (2) `SemanticDeduplicator.find_duplicates` pairwise loop, (3) `VoiceProfileGenerator` loading every body into memory, (4) the `_find_existing` + `_generate_filename` race that surfaces under `ThreadPoolExecutor`.

After this batch, writing 10k fragments to a single subfolder is O(N) total, not O(N²); the dedup pass on 10k embeddings completes in under 30 seconds; voice profile generation peaks under 200 MB RAM.

## Context

Independent of every other batch. Can run in parallel.

The four issues form a coherent file set: `creek/vault/writer.py`, `creek/clean/semantic_dedup.py`, `creek/generate/voice.py`. The first three issues have measurable acceptance criteria; the fourth (race) is a correctness bug exposed by the same code path as PERF-001.

PERF-002 (audit/provenance log rewrite) is in **Batch C**, not here, because it's tangled with the audit-log redesign. If Batch C lands first, the JSONL infrastructure it provides should be reused for `_log_provenance` rather than re-implemented.

**Read these issue files before starting** (in `plans/git-issues/`):
- `PERF-001-vault-writer-quadratic-find-existing.md` — per-directory ID index
- `PERF-003-semantic-dedup-quadratic.md` — vectorise + spatial index
- `PERF-004-voice-exemplar-loads-all-bodies.md` — stream rather than load
- `BUG-006-vault-writer-race-on-concurrent-writes.md` — atomic create + index lock

**Files you will primarily change:**
- `creek-tools/creek/vault/writer.py` — `_find_existing`, `_generate_filename`, new `_index.json`/`.idx.jsonl`
- `creek-tools/creek/clean/semantic_dedup.py` — vectorised pairwise; optional FAISS path
- `creek-tools/creek/generate/voice.py` — streaming exemplar accumulation

**Files to consult:**
- `creek/link/embeddings.py` — uses similar pairwise patterns; verify it doesn't need the same fix (the embedding linker already returns `(i, j, sim)` triples; check whether it iterates pairs in Python or vectorises)
- The Batch C `AuditLog` infrastructure if it has landed — reuse for the per-directory index file

## Output format

Four commits, each with a benchmark or a regression test that proves the fix:

1. **Per-directory ID index for VaultWriter.** A `<dir>/.id-index.json` (or `.id-index.jsonl`) maintained on every write; `_find_existing` is now O(1). Atomic writes via `O_CREAT | O_EXCL` with a counter-suffix retry — eliminates the `_generate_filename` race. Optional `fcntl.flock` on the index for cross-process safety.
2. **Vectorised semantic dedup.** Use `embeddings @ embeddings.T` (after L2 normalisation) for the cosine matrix. Threshold-mask. Emit duplicate pairs as the upper triangle.
3. **Optional FAISS path.** Behind `DeduplicationConfig.use_faiss: bool = False`. If FAISS is unavailable, fall back to the dense matrix. Don't make FAISS a hard dependency.
4. **Streaming voice exemplar accumulator.** Walk fragments lazily, route each fragment's body to a per-register on-disk accumulator (one file per register), drop the body. After the walk, run analysis on the accumulators with bounded memory.

## Examples

The vault-writer scaling test:

```python
def test_vault_writer_constant_time_per_write(tmp_path):
    vault = make_empty_vault(tmp_path)
    writer = VaultWriter(vault)
    durations = []
    for i in range(2000):
        f = make_fragment(id=f"frag-{i:012d}", title=f"t{i}")
        t0 = time.perf_counter()
        writer.write_fragment(f, body=f"body {i}")
        durations.append(time.perf_counter() - t0)
    # Median write time near the end should be within 3x of near the start
    early = statistics.median(durations[10:60])
    late = statistics.median(durations[-50:])
    assert late < 3 * early, f"Per-write time grew: early={early:.4f}s late={late:.4f}s"
```

The dedup scaling test:

```python
def test_semantic_dedup_scales_to_10k(tmp_path):
    rng = np.random.default_rng(0)
    embeddings = rng.standard_normal((10_000, 384)).astype(np.float32)
    dedup = SemanticDeduplicator(threshold=0.85)
    t0 = time.perf_counter()
    pairs = dedup.find_duplicates(embeddings)
    elapsed = time.perf_counter() - t0
    assert elapsed < 30.0, f"dedup took {elapsed:.2f}s for 10k embeddings"
```

The race test:

```python
def test_concurrent_vault_writes_no_loss(tmp_path):
    vault = make_empty_vault(tmp_path)
    writer = VaultWriter(vault)
    fragments = [make_fragment(id=f"frag-{i:012d}", title=f"t{i}") for i in range(200)]
    def write(f): writer.write_fragment(f, body="x")
    with ThreadPoolExecutor(max_workers=8) as ex:
        list(ex.map(write, fragments))
    written = list((vault / "01-Fragments").rglob("*.md"))
    assert len(written) == 200
    ids = {frontmatter.load(str(p))["id"] for p in written}
    assert ids == {f.id for f in fragments}
```

## Requirements

- **Use `/stay-green`** with the scaling/race tests above as Gate 1. They should fail before the fix and pass after.
- **Use `/max-quality-no-shortcuts`** if you're tempted to lower the assert thresholds rather than make the code faster. The 30s / 3x / 200MB numbers are the floor for "good enough at 10k scale" — if you can't hit them, raise it as a sub-issue rather than weakening the test.
- The vault writer index file is operational, not compliance — it can be a plain JSON file written via `tempfile + os.replace`. Don't over-engineer it.
- For FAISS, the import lives behind `if config.use_faiss:` — keep it lazy.
- For the voice-exemplar streaming accumulator, write the on-disk format as JSONL (one exemplar per line). The downstream analysis can iterate the file rather than load it all.
- Maintain `mypy --strict` clean. The vectorised dedup likely needs `numpy.typing.NDArray[np.float32]` annotations — that's fine; add them.
- Maintain ≥90% branch coverage. The new index code should be near 100%.
- The benchmark tests above can be marked `@pytest.mark.slow` so they aren't part of the default unit pass; add a `slow` marker to `pyproject.toml`. Run them in CI's `--all` job.
- Don't change the `VaultWriter` public API surface. Existing callers (which Batch A may have updated to pass `body=`) keep working.

## Definition of done

`./scripts/check-all.sh` exits 0. The three benchmark tests pass on a machine with reasonable specs (CI runner is fine). A 10k-fragment ingestion completes in O(N) wall time. A `creek clean duplicates` run on a 10k embedding matrix finishes in under 30 seconds. `creek report --type voice` peaks below 200 MB RAM on the same vault.
