# BUG-006: `VaultWriter` ID-lookup + filename generation is not atomic

**Severity:** Medium
**Category:** BUG
**Estimated complexity:** M (≤1d)
**Parallelizable with peers in same category:** yes
**Discovered by:** Reading dimension 1 + dimension 6 (race conditions)

## Files affected
- `creek/vault/writer.py:260-339` — `_write_model`, `_find_existing`, `_generate_filename`

## Dependencies
None.

## Blockers
None at single-process scale; matters when LLM classification batches use `ThreadPoolExecutor` (which they do — see `creek/classify/llm.py`) or when the user runs two stages from cron concurrently.

## Reproduction
Two threads each call `VaultWriter.write_fragment` for fragments with different IDs but identical `(date, title)`. Both call `_generate_filename` → both see the same `2025-04-29-foo.md` available → one overwrites the other. Or both call `_find_existing` for an ID, both miss, both proceed to write at slightly different filenames — fine — but the writer rewrites the entire `provenance.json` from scratch (`_log_provenance` line 360-377), and one of the two writes loses the other's entry.

## Analysis

Three race surfaces:
1. **`_find_existing`** scans the directory, opens every `.md`, parses frontmatter. If two writers race, both can return `None` for the same ID and both write distinct files for the same model — duplicates land in the vault.
2. **`_generate_filename`** does `if not (target_dir / filename).exists()` and returns immediately. Classic TOCTOU window. Two writers will pick the same name.
3. **`_log_provenance`** reads the entire JSON, appends, rewrites. Last writer wins; entries are silently lost.

Beyond races, `_find_existing` is also O(N²) per write because it parses every file in the directory on every call (PERF-001).

Confidence: verified.

## Proposed remediation

- Maintain a per-directory `index.json` mapping `id → path` written transactionally with the file. Look up there first; fall back to a directory scan only if the index is missing/stale (rebuild it).
- Use `os.O_CREAT | os.O_EXCL` to write each file: if the path already exists, retry with a counter suffix. Eliminates the `_generate_filename` race.
- For the provenance log, switch to JSONL (one entry per line, opened with `O_APPEND` — POSIX guarantees atomicity for line-sized writes) instead of "load all → mutate → dump all".
- Optionally, use `fcntl.flock` around the index update.

This naturally pairs with PERF-001 (the O(N²) scan is the same dataset).

## Acceptance criteria

- Two `ThreadPoolExecutor` workers writing 100 distinct fragments each to the same directory produce 200 distinct `.md` files with no overwrites.
- Provenance log contains 200 entries — none lost.
- Re-running a write for a fragment whose ID already exists is a no-op (existing path returned).
- `_find_existing` average time per write is O(1) (or O(log N)) once the index is warm.

## References
- `creek/vault/writer.py:260-377`
- BUG-002 (provenance timestamps) and PERF-001 (O(N²) scan) overlap with this issue.
