# PERF-004: `VoiceProfileGenerator` loads every fragment body into memory

**Severity:** Medium
**Category:** PERF
**Estimated complexity:** M (≤1d)
**Parallelizable with peers in same category:** yes
**Discovered by:** Dimension 9

## Files affected
- `creek/generate/voice.py:265-292`

## Dependencies
None.

## Blockers
None for small vaults; will OOM on machines with <1GB free RAM at 10k fragments × 5KB body.

## Reproduction
Run `creek report --type voice --vault <large_vault>`. Memory usage scales linearly with vault size.

## Analysis

`_load_fragment_with_body` (in `voice.py`) is called for every `.md` file in `<vault>/01-Fragments/**` and the result is held in a `buckets` dict keyed by register. Bodies stay in memory for the full pass. For a 10k-fragment vault at 5KB average body, that's 50MB — fine on a developer machine, painful on a low-RAM system, and trivially worse if some sources contain long PDFs/transcripts.

## Proposed remediation

Stream:
- Walk the fragments lazily.
- For each fragment, classify its register, and route to a per-register **on-disk** accumulator (e.g., one file per register collecting exemplars). Drop the body after writing to the accumulator.
- After the walk, run the per-register analysis from disk with bounded memory.

Alternative: keep only the top-K exemplars per register in a heap; drop everything else as you go.

## Acceptance criteria

- Voice profile generation on a 10k-fragment vault peaks below 200MB RAM.
- Result is identical to the in-memory path for a small reference vault.
- New test confirms peak memory.

## References
- `creek/generate/voice.py:265-292`
