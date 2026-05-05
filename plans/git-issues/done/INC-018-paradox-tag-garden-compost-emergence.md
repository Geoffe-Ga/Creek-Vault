# INC-018: Emergence infrastructure (paradox / unnamed digest / compost / synchronicity / tag garden) has implementations but spec-divergent docs

**Severity:** Medium
**Category:** INC
**Estimated complexity:** M (≤1d) — primarily verification + docs
**Parallelizable with peers in same category:** yes
**Discovered by:** Dimension 5

## Files affected
- `creek/generate/paradox.py`, `creek/generate/synchronicity.py`, `creek/generate/unnamed.py`, `creek/generate/compost.py`, `creek/generate/tags.py`
- `00-Creek-Meta/Ontology/creek_ontology_agent_prompt.md` §10
- `creek-tools/docs/generation.md`

## Dependencies
None.

## Reproduction
The ontology spec §10 documents specific behaviours for each:
- §10.1 Unnamed Digest — weekly cadence, embedding similarity *within* unnamed fragments to surface clusters
- §10.2 Paradox Preservation — *do not resolve*, link both, tag `#paradox`
- §10.3 Synchronicity — `similarity > 0.9`, *different* source types, `> 30 days apart`, exclude "still working on X" noise
- §10.4 Compost — preserve abandoned threads with reasoning
- §10.5 Tag Garden — quarterly review, tag rapid growth detection, suggested consolidation

Each of these has exact criteria. The code exists; the user-facing docs are sparse on the precise behaviour. Whether each of the criteria above is actually implemented needs verification.

A spot check: `docs/linking.md:69` describes synchronicities only as "surprising" without naming the criteria. `docs/generation.md` mentions `--type synchronicity` but not the precise filter. A user can't tell whether a missing synchronicity is a bug or intended behaviour.

## Analysis

This is a "implemented but undocumented" cluster. It needs two passes:

1. **Verification.** For each criterion in spec §10.1-10.5, find the matching code path. If missing, file a BUG / INC sub-issue.
2. **Documentation.** Once verified, document the precise behaviour in `docs/generation.md` (or split into `docs/emergence.md`).

I verified one criterion: synchronicity similarity threshold. `creek/generate/synchronicity.py` and `creek/models.py:Synchronicity` reference `similarity > 0.9` in docstring and the model's docstring lines 472-476. So that criterion is at least documented at the model level — but a CLI user reading `docs/` doesn't see it. Other criteria (`> 30 days apart`, source-type difference) are similarly buried.

## Proposed remediation

For each of §10.1-10.5:
- Confirm the criteria are implemented (open a BUG if not).
- Surface the criteria in user-facing docs.
- Add a unit test that pins the exact thresholds (so a future regression that changes them lights up red).

Add `creek-tools/docs/emergence.md` aggregating the §10 features. Cross-reference from `docs/generation.md`.

## Acceptance criteria

- For each §10 criterion, there is a matching test and a doc paragraph.
- A user reading `docs/` knows the synchronicity threshold is 0.9 cosine, > 30 days, source mismatch.
- A regression PR that changes the threshold without updating the doc fails the test.

## References
- `00-Creek-Meta/Ontology/creek_ontology_agent_prompt.md` §10
- `creek/generate/{synchronicity,paradox,unnamed,compost,tags}.py`
- `creek-tools/docs/generation.md`, `linking.md`
