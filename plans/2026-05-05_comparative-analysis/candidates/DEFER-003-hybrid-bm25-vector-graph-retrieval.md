# DEFER-003: Hybrid BM25 + Vector + Graph Retrieval at Scale

**Verdict:** DEFER
**Source system:** Karpathy LLM Wiki community elaboration ([LLM Wiki v2 by rohitg00](https://gist.github.com/rohitg00/2067ab416f7bbe447c1977edaaa681e2))
**Affects:** Creek Vault data layer
**Roadmap target:** unscheduled
**Estimated complexity:** L
**Conflicts with non-negotiables?** none

## What it is

The "LLM Wiki v2" extension gist proposes hybrid retrieval over a personal wiki: BM25 (keyword), embeddings (semantic), and graph traversal (typed-relationship), combined with reranking. The argument: above ~200–400 pages of wiki, in-context navigation breaks down and you need real retrieval.

## Why it's interesting

The premise is correct but bounded. At small scale, Karpathy's `index.md` approach works (ADOPT-007). Creek's compiled-layer is intended to stay small — the lint pass surfaces fragmentation as a quality signal (ADOPT-007 pins this). But for the *fragment* layer underneath the compiled layer, retrieval matters: tens of thousands of fragments, embedding-based resonance discovery already in place, no full-text-search component yet.

A hybrid BM25 + embedding retrieval over fragments would help mining strategies — `creek mine --strategy resonance-chain` could be sharper if it had keyword filtering alongside cosine similarity.

## Why DEFER, not ADOPT

Three reasons:

1. **Embedding-based resonance is doing the work today.** The mining strategies, synchronicity detection, and Eddy formation all run on cosine similarity over `sentence-transformers` embeddings. Adding BM25 is additive but not load-bearing.
2. **Graph traversal is partially covered by the topology candidate (ADAPT-001).** Leiden clustering provides a graph view of fragment relationships. Whether that's enough for "graph traversal" or whether explicit typed-relationship queries are needed is open.
3. **The compiled layer should not need this.** If `creek lint` keeps the compiled layer context-window-sized (ADOPT-007), retrieval at the compiled layer is unnecessary. Adding hybrid retrieval is solving a problem at the wrong layer.

The right time to revisit:
- When the fragment count grows past ~50K and `creek mine` becomes noticeably slow or imprecise.
- When CrawDad's conversational responses need keyword-anchored citations ("you wrote on March 14 about X") and embedding-only retrieval misses dates and proper nouns.

## Dependencies

- Adjacent to: ADAPT-001 (Leiden as graph view — partial substitute), ADOPT-007 (`index.md` discipline keeps the compiled layer small enough not to need this).

## Acceptance criteria

N/A — deferred. Trigger conditions documented above.
