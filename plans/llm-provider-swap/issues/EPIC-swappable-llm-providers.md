# EPIC: Swappable LLM providers (Anthropic / OpenAI / Gemini), keys from env

## Summary

Make the cloud LLM backend selectable — Anthropic, OpenAI, or Google
Gemini — for both `creek-tools` and CrawDad, via one config string per
package. API keys are discovered from the environment by each provider's
SDK and never written into the repo, config files, logs, or errors.
Ollama (local) stays the `creek-tools` default.

## Why

Today the provider is effectively hard-wired:
- `creek-tools` has a real but two-way dispatch (`anthropic` vs Ollama).
- CrawDad is hard-wired to `anthropic.AsyncAnthropic`.

Operators should be able to swap providers without code changes and
without ever storing a key in the repo.

## Architectural decision (load-bearing)

**Keep `creek-tools` and CrawDad decoupled.** They are siblings
(FEAT-013): CrawDad imports nothing from `creek-tools`, talking to it
only over MCP stdio. We do **not** introduce a shared `creek.llm`
package. Each package gets its own provider abstraction — **sync** in
`creek-tools`, **async** in CrawDad — mirroring each other's structure
(a normalized `Completion` type + a `complete()` contract) without
sharing a module. DRY holds within each package; the ~30–40-line
per-provider wrapper overlap is the accepted price of the clean
boundary. Revisit only if the duplicated bodies grow real logic.

## Constraints (apply to every child issue)

1. No `api_key` field on any config model — SDKs read env
   (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GOOGLE_API_KEY`).
2. Re-raise native SDK errors as `RuntimeError(type(exc).__name__)` —
   never leak request state.
3. Cloud-egress consent is provider-neutral; Ollama exempt.
4. No model-ID literals outside config modules.
5. Quality bar unchanged (coverage ≥90%, docstrings ≥95%, complexity
   ≤10, mypy strict, ruff/bandit clean).

## Children (sequenced, tracer-code; each ships green)

- [ ] #1 Normalize `Completion` + `LLMProvider` Protocol (creek-tools)
- [ ] #2 Provider factory; route orchestrator + author client through it
- [ ] #3 Generalise the cloud-consent gate (provider-neutral)
- [ ] #4 `OpenAIProvider` (sync) + optional dep + tests
- [ ] #5 `GeminiProvider` (sync) + optional dep + tests
- [ ] #6 CrawDad async provider abstraction + factory (refactor only)
- [ ] #7 CrawDad OpenAI + Gemini providers + config selection
- [ ] #8 Docs + ADR (env-var matrix, decoupling decision)

## Done-Done

- `creek-tools`: `provider: openai|gemini|anthropic|ollama` in
  `creek_config.yaml` selects the backend; classification + author desk
  both honor it; keys only from env.
- CrawDad: `CRAWDAD_PROVIDER` selects the backend; router + composer both
  honor it; key required is the one matching the provider.
- Both READMEs document the env-var matrix; an ADR records the
  decoupling decision. All gates green in both packages.
