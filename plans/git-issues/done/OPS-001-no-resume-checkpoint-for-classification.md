# OPS-001: LLM classification has no checkpoint/resume — a crash at fragment 5000 of 10000 loses everything

**Severity:** High
**Category:** OPS
**Estimated complexity:** M (≤1d)
**Parallelizable with peers in same category:** yes
**Discovered by:** Dimension 7; confirmed by parallel agent

## Files affected
- `creek/classify/llm.py:635-708` — `LLMClassifier.classify_batch`

## Dependencies
None.

## Blockers
None for short runs; severe for the documented 10k-fragment workflow.

## Reproduction
Start `creek classify --method llm` against a 10k-fragment vault. Kill the process at any point. The next run starts from scratch.

## Analysis

`docs/classification.md` documents:
> Latency on a CPU is ~2–4 s per fragment; expect a few hours for a vault of 10k fragments.

A few hours is enough wall time to hit any of: laptop closing, network blip (Anthropic path), Ollama crashing, OOM, system update, power loss. The current implementation accumulates results in a `ThreadPoolExecutor` future map and writes nothing until the batch completes. There is no checkpoint file, no per-fragment write, no resume flag.

For the Anthropic path, this also wastes paid LLM calls — restart costs $1–30 in tokens that have already been spent.

Confidence: verified — read `LLMClassifier.classify_batch`.

## Proposed remediation

1. Write each classified fragment back to its `.md` file as soon as it's classified (rather than at end-of-batch). This is the cheap fix and most of the value.
2. Maintain a per-vault `<vault>/00-Creek-Meta/Processing-Log/llm-progress.json` with the IDs already classified during the current run. On startup, skip those.
3. Add `creek classify --resume` that's a no-op if no progress file is present, otherwise picks up from the last completed fragment.
4. Document the resume contract.

## Acceptance criteria

- After a SIGKILL during classification, re-running the same command resumes from where it left off.
- A 10k-fragment classify can be paused mid-run and continued without loss.
- Anthropic path doesn't re-pay for fragments already classified.
- Progress is visible in tqdm (already there, but plumb through restarts).

## References
- `creek/classify/llm.py:635-708`
- `creek-tools/docs/classification.md` (latency claim)
