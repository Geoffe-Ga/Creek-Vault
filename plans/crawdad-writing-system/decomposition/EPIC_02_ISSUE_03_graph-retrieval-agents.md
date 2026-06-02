## Role

You are a senior Python engineer working in `creek/author/agents.py`, fluent in the compiled-layer
query path, the embeddings/link modules, and Obsidian backlink structure.

## Goal

Replace the **Graph** and **Retrieval** specialist stubs with real implementations: the Graph agent
navigates the compiled layer + Obsidian backlinks + frequency/wavelength indexes (bounded
breadth/depth walk); the Retrieval agent does semantic retrieval over `01-Fragments/`, `09-Reference/`,
and `11-Other-Authors/`. Both return structured, provenance-tracked `EvidenceBundle`s.

## Context

- **Parent epic:** #EPIC_02_NUMBER
- **Predecessor issue(s):** #EPIC_02_ISSUE_02_NUMBER (medium contract drives specialist weighting).
- **SPEC section:** §4.1 (Graph/Retrieval rows), §6 (knowledge-graph navigation), §13 open question #4 (bounds).
- **Files involved:**
  - `creek-tools/creek/author/agents.py` — real Graph + Retrieval agents.
  - reuse `creek/link/embeddings.py`, the `creek query` path, backlink parsing.
  - `creek-tools/tests/` — agent tests on a fixture vault.
- **Prior decisions:** Open question #4 default — backlink walk **breadth 25 / depth 2**, relevance-pruned; make these config-bounded, not hard-coded. Evidence is structured (claims + `source_fragments`), never prose. `11-Other-Authors/` evidence carries its author attribution for downstream citation.
- **State of the world:** Stubs from ISSUE_01 return mock evidence. Embeddings + compiled-layer query already exist and should be reused, not reinvented.

## Output Format

A single PR containing:

- [ ] Real Graph agent: seed → bounded backlink walk → structured evidence with provenance.
- [ ] Real Retrieval agent: semantic retrieval across raw + reference + `11-Other-Authors/`.
- [ ] Tests: deterministic evidence bundles on the fixture; walk respects breadth/depth bounds; other-author evidence retains attribution.

## Examples

```python
ev = graph_agent.gather(seed="F6-Pluralism", contract=research, vault=fixture)
assert ev.claims and all(c.source_fragments for c in ev.claims)
assert ev.walk_stats.max_depth <= contract.graph_depth_bound   # default 2
```

## Constraints

**Scope fence:** Do not implement the Ontology agent (ISSUE_04), synthesis/voicing (ISSUE_05), or
reflection (ISSUE_06). No new embedding models — reuse `link/embeddings.py`.

**Anti-bypass (verbatim, non-negotiable):**

> No `noqa`, `# type: ignore`, `pylint: disable`, `eslint-disable`, or equivalent
> linter/type-checker silencers. Fix the root cause. The only exception is the documented 4-line
> escape hatch (third-party library bug / language-version compatibility / benchmarked
> performance necessity / generated code) — and it must include the reason, a reference URL, an
> alternative considered, and a review date. See the `max-quality-no-shortcuts` skill.

**Tracer-code invariant:** The desk still runs end-to-end; Ontology/Voice/Reflection remain stubs
and the pipeline stays green with two real specialists feeding them.

## Definition of Done (stay-green)

- [ ] All new and existing tests pass (`cd creek-tools && ./scripts/test.sh --all`).
- [ ] `pre-commit run --all-files -c creek-tools/.pre-commit-config.yaml` is clean.
- [ ] Coverage on changed lines ≥90% branch; docstrings ≥95%; complexity ≤10; MyPy strict.
- [ ] Walk bounds + determinism proven by test.
- [ ] PR body includes `Refs #EPIC_02_NUMBER` and `Closes #THIS_ISSUE_NUMBER`.
- [ ] Latest Claude reviewer `Verdict:` on HEAD is `LGTM`.

## Labels

`spec-decomposition`, `core`, `author-desk`
