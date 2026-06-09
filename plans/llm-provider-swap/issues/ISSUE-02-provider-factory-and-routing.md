# ISSUE #2 — Provider factory; route orchestrator + author client through it (creek-tools)

**Epic:** Swappable LLM providers · **Sequence:** 2/8 · **Risk:** low–medium · **Depends:** #1

## Role
Python library engineer in `creek-tools/`, max-quality bar, `./scripts/*`.

## Goal
Collapse the scattered `config.provider == "anthropic"` branching into a
single factory, and route both consumers (classification orchestrator and
author desk) through the `LLMProvider` Protocol. Still Anthropic + Ollama
only — no new providers yet.

## Context
- `creek/classify/llm/orchestrator.py` branches on
  `config.provider == self.ANTHROPIC_PROVIDER` in four places:
  `__init__` (warning), `_check_availability`, `_invoke_llm`,
  `invoke_prompt_with_metadata`. It also owns `_get_anthropic_provider`,
  `_check_anthropic_availability`, `_call_ollama`.
- `creek/author/client.py` `AuthorLLMClient.from_config` constructs
  `AnthropicProvider(...)` directly.
- `LLMConfig.provider` is a free string defaulting to `"ollama"`.
- Wrap `call_ollama` / `check_ollama_available` as an `OllamaProvider`
  class implementing `LLMProvider` so the factory returns one uniform
  type.

## Deliverables
1. `creek/classify/llm/providers/ollama.py` (or in-module): `OllamaProvider`
   implementing `LLMProvider` (sync `complete`, `available` via the
   existing health check). Move the `"mistral"` sentinel/model-resolution
   logic into the relevant provider classes.
2. `creek/classify/llm/providers/__init__.py`: `build_provider(config:
   LLMConfig) -> LLMProvider` — a registry `{"anthropic": ...,
   "ollama": ...}`; unknown provider → clear `ValueError`.
3. `orchestrator.py`: `LLMClassifier.__init__` calls
   `self._provider = build_provider(config)`; delete the four branches and
   the `_get_anthropic_*` / `_call_ollama` helpers; delegate `available`,
   `invoke_prompt`, `invoke_prompt_with_metadata` to `self._provider`.
   Keep the `ANTHROPIC_CLOUD_WARNING` emission (driven off the provider's
   identity / a `is_cloud` flag, not a string compare here).
4. `author/client.py`: `from_config` calls `build_provider(effective)`.

## Constraints
- No behavior change for `anthropic` or `ollama` users. The cloud warning
  must still fire for the Anthropic path.
- No new dependencies. Complexity ≤10 — the factory keeps branches small.
- TDD first: factory returns the right class per string; orchestrator
  routes a fake provider's `complete` through unchanged.

## Acceptance
- `./scripts/check-all.sh` exit 0; existing orchestrator/author tests green
  (adjust mocks to the provider seam, not behavior).
- Grep shows no remaining `== "anthropic"` / `ANTHROPIC_PROVIDER` literal
  branching in `orchestrator.py`.
