# ADR-0003: Two parallel LLM-provider abstractions, no shared package

- **Status**: Accepted
- **Date**: 2026-06-09
- **Driving issues**: Epic #603 (swappable LLM providers) and its children
  #604–#611.

## Context

Both `creek-tools` and CrawDad needed a selectable cloud LLM backend
(Anthropic / OpenAI / Gemini, with Ollama as the local default in
`creek-tools`) instead of a hard-wired provider. The obvious DRY instinct is to
extract one shared `creek.llm` package that both import.

Two facts make that the wrong call here:

1. **They are siblings, not parent/child (FEAT-013).** CrawDad imports *nothing*
   from `creek-tools`; it talks to it only over MCP stdio. Introducing a shared
   importable module would create a source-level coupling the architecture has
   deliberately avoided.
2. **The two pipelines have different concurrency models.** `creek-tools` is
   synchronous (batch ingestion/classification); CrawDad is asynchronous
   (discord.py + MCP). A shared `complete()` contract would have to be either
   sync or async, forcing one side into awkward bridging.

The repeated surface is small: a normalized `Completion` result type and a
per-provider wrapper (~30–40 lines each) that maps the vendor SDK's
request/response shape, redacts errors to `RuntimeError(type(exc).__name__)`,
and maps `finish_reason`/usage. That overlap is structural, not behavioral —
the bodies are short and stable.

## Decision

Keep **two parallel provider abstractions, one per package, with no shared
module:**

- `creek-tools`: `creek/classify/llm/` — a **synchronous** `LLMProvider`
  Protocol, a normalized `Completion`, a `build_provider(config)` registry, and
  per-provider classes (Anthropic, Ollama, OpenAI, Gemini). Selection is via
  `creek_config.yaml::llm.provider`. The cloud-egress consent gate
  (`CREEK_CLOUD_CONSENT`, legacy `CREEK_ANTHROPIC_CONSENT` alias) is
  provider-neutral and shared *within* the package.
- CrawDad: `crawdad/crawdad/llm/` — an **asynchronous** `AsyncLLMProvider`
  Protocol mirroring the same `Completion` shape, a `build_async_provider(config)`
  factory, and per-provider async classes. Selection is via the
  `CRAWDAD_PROVIDER` env var.

Each package owns its model-ID literals (in its `config`/config module) and
reads keys from the environment via the vendor SDK — keys are never stored on a
config object, written to YAML, committed, or logged.

DRY holds *within* each package; the cross-package wrapper duplication is the
accepted price of the clean sibling boundary.

## Consequences

- **Positive**: the FEAT-013 decoupling stays intact; each side picks the
  concurrency model that fits; adding a provider is a one-line registry entry
  plus a short wrapper on each side; no cross-package version lockstep.
- **Negative**: ~30–40 lines of conceptually-similar wrapper code exist twice,
  and a new provider must be added in both places to reach both surfaces.

## Revisit when

The duplicated wrapper bodies grow **real, divergent logic** (retry/backoff
policy, streaming, tool-calling, prompt-cache orchestration) rather than thin
SDK-shape mapping. At that point a shared library — published and versioned,
*not* a direct import across the sibling boundary — becomes worth the coupling
cost. Until then, the duplication is cheaper than the abstraction.
