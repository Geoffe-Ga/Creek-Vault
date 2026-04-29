# OPS-004: Long-running stages (linking, indexing, voice generation) have no progress UX

**Severity:** Low
**Category:** OPS
**Estimated complexity:** S (≤2h)
**Parallelizable with peers in same category:** yes
**Discovered by:** Dimension 7

## Files affected
- `creek/link/embeddings.py`, `creek/link/threads.py`, `creek/link/eddies.py`
- `creek/generate/voice.py`, `creek/generate/wavelength.py`, `creek/generate/indexes.py`

## Dependencies
None.

## Reproduction
Run `creek link --vault <vault> --method embeddings` against a 1k-fragment vault. The command runs silently for many seconds with no output until completion.

## Analysis

`creek/redact/scanner.py` and `creek/classify/llm.py` correctly use `tqdm` for batch operations. The linking modules and voice/wavelength generators do not. For operations that take minutes, a user can't tell whether the process is alive, blocked, or hanging.

`docs/linking.md` documents "a 10k-fragment vault rebuilds in ~10–20 minutes" — that's exactly the wall-time range where progress matters most.

## Proposed remediation

Wrap every per-fragment loop in `tqdm` with `desc=` and `total=` set. Use `disable=not sys.stderr.isatty()` so tests and pipes don't get progress noise.

For multi-stage pipelines (e.g., embedding → resonance → eddy → thread), use a top-level `tqdm` with stage descriptions, plus per-fragment inner bars.

## Acceptance criteria

- Every long-running stage shows progress in interactive terminals.
- Non-TTY runs don't emit progress (clean log files).
- A test that runs against tty-emulation captures progress output.

## References
- `creek-tools/docs/linking.md` (latency claim)
- `tqdm` docs
