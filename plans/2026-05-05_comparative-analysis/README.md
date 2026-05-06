# Comparative Analysis: Creek Vault + CrawDad vs. the Knowledge-Graph Landscape

Read-only landscape analysis (May 2026) comparing Creek Vault and the planned CrawDad agent against four reference systems: Graphify, Karpathy's LLM Wiki pattern, AlfredOS / `alfred-vault`, and Dontoh's Alfred. No source code is modified; this directory is decision support, not implementation.

## Reading order

1. **[`LANDSCAPE.md`](LANDSCAPE.md)** — prose framing, category map, values vs. mechanics. **Start here.**
2. **[`DELTA-MATRIX.md`](DELTA-MATRIX.md)** — dimensional comparison table plus synthesis. Read second; the synthesis paragraphs are the most actionable section.
3. **[`INTEGRATION-PLAN.md`](INTEGRATION-PLAN.md)** — `v1.0 → v1.3+` roadmap, candidates index, CrawDad design implications by interaction mode, voice-fidelity stack, distinctiveness watchlist, open questions for the human.

Then drill into the supporting material as needed:

- **[`systems/`](systems/)** — one brief per reference system (`graphify.md`, `karpathy-llm-wiki.md`, `alfredos.md`, `alfred-dontoh.md`).
- **[`candidates/`](candidates/)** — 23 adoption candidates: 8 ADOPT, 8 ADAPT, 4 REJECT, 3 DEFER. The candidates index in `INTEGRATION-PLAN.md` is the navigable entry point.

## v1.0 prerequisite

The analysis flags spec/implementation drift on phase, mode, and frequency taxonomy as a v1.0 prerequisite for any compile-then-query adoption work. **Tracked as [`INC-019`](../git-issues/INC-019-spec-impl-drift-phase-mode-frequency-taxonomy.md) / [GH #201](https://github.com/Geoffe-Ga/Creek-Vault/issues/201)** and listed in the [pre-launch must-fix High set](../git-issues/INDEX.md#high-ship-only-with-explicit-acknowledgement).

## Source verification window

Reference-system characterisations reflect public materials available in early May 2026. Per-source URLs are listed in each `systems/{slug}.md` brief; if a candidate is acted on more than a few months from now, re-verifying the cited URLs against current upstream is cheap and worth doing.
