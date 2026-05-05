# Graphify

## Source URLs

- Repo: <https://github.com/safishamsi/graphify>
- Docs: <https://graphify.net>
- Architecture: <https://github.com/safishamsi/graphify/blob/v3/ARCHITECTURE.md>
- How it works: <https://github.com/safishamsi/graphify/blob/v6/docs/how-it-works.md>
- READMEs: [v3](https://github.com/safishamsi/graphify/blob/v3/README.md), [v4](https://github.com/safishamsi/graphify/blob/v4/README.md), [v5](https://github.com/safishamsi/graphify/blob/v5/README.md)
- Token-regression issue: <https://github.com/safishamsi/graphify/issues/580>
- GoPenAI walkthrough (Mustafa Genc, Apr 2026): <https://blog.gopenai.com/graphify-build-a-knowledge-graph-from-your-entire-codebase-without-sending-your-code-to-anyone-1b6924474b50>

## What it is

Graphify is an open-source skill (slash command) installable into Claude Code, Codex, Cursor, Gemini CLI, Copilot, and Aider. It walks a folder tree (code, docs, papers, images, audio, video) and produces three artifacts: a NetworkX graph, an interactive HTML visualization, and a markdown audit report (`GRAPH_REPORT.md`). The agent reads the audit report before tool calls via a `PreToolUse` hook, so subsequent agent operations are graph-aware.

## Architecture

Three-pass pipeline with a strict locality boundary:

```
detect → extract (3 passes) → build_graph → cluster (Leiden) → analyze → report → export
```

- **Pass 1 (local, no LLM):** tree-sitter ASTs across 25+ languages — classes, functions, imports, call graphs, docstrings, and comments tagged `NOTE:`/`WHY:`/`HACK:` are extracted deterministically.
- **Pass 2 (local, no API):** `faster-whisper` transcribes audio/video. Prompt is "domain-aware," seeded by the current top-ranked nodes in the in-progress graph.
- **Pass 3 (Claude API, parallel):** markdown, PDFs, images, and transcripts go to Claude subagents that emit JSON fragments (nodes, edges, groups). **Only this pass costs tokens.**

Data model: nodes carry `{id, label, file_type, source_file, source_location}`; edges carry `{source, target, relation, confidence_tier ∈ {EXTRACTED, INFERRED, AMBIGUOUS}, confidence_score}`. Hyperedges (3+ nodes) live in `G.graph["hyperedges"]`. Output writes to a `graphify-out/` directory designed to be git-committable, with a custom merge driver to keep `graph.json` conflict-free.

Clustering: Leiden via `graspologic` over the resonance graph. "Similarity" is not vector cosine; instead, the LLM emits explicit `semantically_similar_to` edges during Pass 3, and similarity becomes a graph-traversal question (shortest path, k-hop neighborhood, Leiden community co-membership). Recall is bounded by what Claude noticed during one extraction pass.

## Wins

- **Privacy by pipeline-split.** Source code never reaches the network because the only network-egress pass (Pass 3) sees only docs/papers/images/transcripts by construction. This is a real engineering boundary, not a hand-wave.
- **Deterministic-first.** Tree-sitter does the cheap, reliable, no-LLM work before any token spend. The Pass-1/Pass-3 split is the architectural lesson worth borrowing even if the Leiden/topology side isn't.
- **Audit report as primary artifact.** `GRAPH_REPORT.md` is structured: summary counts, community hubs, god nodes, surprising connections, hyperedges, suggested questions. The agent reads it before tool calls; the human reads it to understand what the system thinks the corpus is about.
- **Confidence tiers on edges.** `EXTRACTED` (1.0), `INFERRED` (0.55–0.95 with score), `AMBIGUOUS` (flagged for human review) — a small, legible grammar that beats binary edge/no-edge.
- **Slash-command surface.** `/graphify .`, `/graphify --update`, `/graphify query "..."`, `/graphify path A B`, `/graphify explain X` — ten or so commands cover the full lifecycle. Distribution as a skill across multiple agent platforms is more thoughtful than most knowledge tools.
- **`.graphifyignore`-style scoping.** Standard ignore-file pattern for excluding paths.

## Costs

- **The "no embeddings" claim is rhetorically misleading.** Similarity recall is bounded by what one extraction-time LLM pass noticed. Embeddings exist precisely because LLM-emitted edges miss things. For text where the connection is wavelength-phase resonance across no surface vocabulary, topology alone won't find it.
- **The 71.5× token efficiency claim is corpus-cherry-picked.** Baseline is "read all 52 files into context per query," which no modern coding agent does. Third-party reproductions report 499× on different corpora; [issue #580](https://github.com/safishamsi/graphify/issues/580) reports the *opposite* effect (more tokens, not fewer) in real Claude Code sessions. Treat as marketing-grade.
- **"Multimodal" overpromises.** Video means audio-only (Whisper); diagrams and screenshots are treated as generic images via Claude vision; no frame-level video understanding documented.
- **Privacy claim glosses two leaks.** Symbol names, file paths, and rationale comments (`NOTE:`/`WHY:`/`HACK:`) become first-class graph nodes and almost certainly enter Pass-3 prompts. "Source code never leaves the machine" is true of the bytes; symbol-level identifiers leave.
- **Stale graph risk.** A graph that drifts from the corpus becomes a high-authority misinformation source. `--update` and post-commit hooks help; nothing forces them.
- **In-memory NetworkX won't scale to multi-million-node monorepos.** Not a Creek problem (personal scale) but worth knowing.

## Relevance to Creek Vault, CrawDad, or both

**Creek Vault, primarily.** Three Graphify ideas are directly applicable:

1. **The Pass-1/Pass-3 deterministic-first split** maps onto Creek's existing rules-then-LLM classification. Already partially adopted (`creek classify --method rules` then `--method llm` on residue); worth making explicit and audit-able.
2. **Leiden clustering over the resonance graph** is the canonical "topology complement to embeddings" candidate — *not* a replacement for embedding-based resonances, but a community-detection layer over them that produces an alternative view of eddies.
3. **The `GRAPH_REPORT.md` audit-as-artifact** maps onto a missing piece: Creek has no single document that summarizes vault state. A `creek state` (or `creek lint --report`) producing structured sections (god-fragments, surprising resonances, growing eddies, drift warnings, suggested questions) would be a v1 deliverable.

The **multimodal scope** (audio/video via local Whisper) is a roadmap candidate: voice memos and podcast episodes are explicitly listed in the spec's "other potential sources" but not implemented.

**CrawDad, secondarily.** The `/graphify` slash-command grammar is a useful prior art for `/creek` and `/crawdad` slash commands inside Discord. The `PreToolUse` hook pattern (read the audit report before any operation) maps onto CrawDad's session-start: load the latest `creek state` snapshot before answering anything.

What to **not** borrow: the "no embeddings, no vector DB" position is a REJECT — Creek's embedding-based resonance is doing semantic work topology can't replicate, and the user has explicitly committed to keeping it. The 71.5× claim and any rhetoric leaning on it should be ignored entirely.
