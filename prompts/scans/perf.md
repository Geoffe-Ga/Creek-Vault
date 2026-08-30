<!--
  Scan definition consumed by the scan-issue-writer skill via the reusable
  _claude-scan.yml core. Performance sweep of this repo: find the
  highest-impact perf defects at HEAD and hand each to the skill as a finding.
  Follows the same 6-component framework as the issues it produces.
-->

## Role
Performance engineer for this repo (Python packages: the Creek pipeline under
`creek-tools/creek/`, the MCP server under `creek-tools/creek_mcp/`, and the
CrawDad Discord bot under `crawdad/crawdad/` — see `CLAUDE.md` and
`creek-tools/CLAUDE.md`). You find the perf defects that actually cost the
pipeline wall-clock time, API spend, or memory at real vault scale and hand
each, with reproducible evidence, to the scan-issue-writer skill.

## Goal
Surface the highest-impact performance defects present at HEAD so each becomes a
tracked, agent-ready issue. Prefer a few well-evidenced findings over a long
speculative list. A run that finds none is a valid, successful, zero-issue run.

## Context
- Title-slug prefix: `[scan:perf]`
- Priority label for this scan (workflow input): `P2`
- First-party source only — `creek-tools/creek/`, `creek-tools/creek_mcp/`,
  `crawdad/crawdad/`.
- Record the SHA with `git rev-parse HEAD` before scanning; every issue cites it.
- Scale anchor: real vaults reach ~35k fragments. Anything O(n²) in fragment
  count, or that materializes all-pairs state, has already been observed to
  stall or get OOM-killed at that size. Judge every finding against 35k, not
  the toy test vault.
- What counts as a finding:
  - **O(n²) pairwise loops in linking/clustering** — Python-level all-pairs
    loops over fragments in embedding-similarity scoring, DBSCAN eddy
    formation, or paradox/thread candidate generation. Recommend top-k /
    neighborhood bounding or a vectorised formulation. Evidence must be the
    loop citation plus the growth argument (or a timing at two vault sizes).
  - **Missing vectorization** — per-element Python loops over embedding
    vectors or similarity rows where a single numpy matrix operation would do.
  - **Unbatched LLM / embedding API calls** — a per-fragment API call inside a
    loop where the provider offers a batch endpoint or the call accepts a list;
    this costs both latency and spend.
  - **Missing caching / memoization** — recomputing embeddings, parsed
    frontmatter, or classification features for unchanged fragments instead of
    reusing a persisted/index result.
  - **Memory blowups at vault scale** — materializing a full n×n similarity
    matrix or loading every fragment body into memory at once where a
    streaming / chunked / sparse approach would hold at 35k fragments.
  - **Re-reading the vault per stage** — a pipeline stage that re-walks and
    re-parses the whole vault when a prior stage already produced the needed
    parse or index.
- Known-hot paths to weight first: the link stage (resonance scoring, eddy
  DBSCAN, thread building), classification over the full vault, embedding
  index construction, and draft-time retrieval.
- Exclusions (NOT findings): generated code, lockfiles, vendored deps, build
  output, and anything already covered by an open `[scan:perf]` issue (the
  skill dedupes).

## Output Format
Findings as a JSON list, one object per finding:

**`symbol` is mandatory whenever the finding names a function, method or class** (#1651). Declare it as a field — `symbol: "name"` or `symbols: ["a", "b"]` — never only inside the title string, and never a name paraphrased from surrounding code. Every declared symbol is verified against the scan SHA's blob by `creek-tools/scripts/verify-scan-citations.sh` before any issue is filed, and a name that has no definition at that SHA blocks the create. A finding that legitimately names no symbol (a whole-module or config finding) simply omits the field. A symbol you are PROPOSING to create — a refactor target — belongs in `fix_strategy`, not in `symbol`.


```json
{
  "slug": "perf-link-pairwise-similarity",
  "title": "O(n^2) Python loop computing pairwise similarity in link.resonance",
  "severity": 4,
  "file": "creek-tools/creek/link/resonance.py",
  "lines": "142-160",
  "evidence": "nested for-loop over all fragment pairs calling cosine per pair; 35k fragments = ~600M pairs (observed kill at 35k per repo memory)",
  "before_after_sketch": "pairwise loop → normalized embedding matrix + single numpy matmul with top-k selection"
}
```

Severity is 1–5. It orders findings against `max_issues`; the priority label
comes from the workflow input.

## Examples
- A nested loop over all fragment pairs calling a per-pair cosine → severity 4;
  sketch shows the numpy matrix formulation with top-k bounding.
- A classify loop issuing one embedding API request per fragment when the
  client accepts a batch of texts → severity 4; sketch shows chunked batch
  calls.
- The index stage re-reading and re-parsing every vault file that the link
  stage parsed moments earlier → severity 3; sketch shows passing the parsed
  fragment set (or a cached parse) between stages.

## Constraints
- Read-only analysis; never modify code.
- Evidence must be reproducible from tool output (a timing, a profile, a memory
  trace) or a direct code citation with a concrete growth argument at 35k
  fragments. No speculative "this might be slow" — if you cannot show the cost,
  it is not a finding.
- Skip anything already covered by an open `[scan:perf]` issue.
- Respect `max_issues`; defer the overflow to the run summary.
