# ISSUE #6 — CrawDad async provider abstraction + factory (refactor only)

**Epic:** Swappable LLM providers · **Sequence:** 6/8 · **Risk:** medium · **Depends:** none (parallel to #1–#5)

## Role
Python engineer in `crawdad/` (async, discord.py + MCP), max-quality bar,
`./scripts/*`. CrawDad stays **decoupled** from creek-tools — no import
from it; mirror the structure, don't share the module.

## Goal
Introduce an async provider abstraction inside CrawDad and route the
router + composer through it, **with Anthropic as the only provider** —
a pure refactor that ships green and sets up #7.

## Context
- `crawdad/crawdad/cli.py` `_build_agent_components` builds
  `anthropic.AsyncAnthropic(api_key=config.anthropic_api_key)` and passes
  it into `IntentRouter` (`router.py`) and `SonnetComposer`
  (`composer.py`).
- `router.py` / `composer.py` call `self._client.messages.create(...)` and
  `except anthropic.AnthropicError`. Both accept the client via DI
  (typed loosely), which helps.
- Model IDs live only in `config.py` (`DEFAULT_ROUTER_MODEL`,
  `DEFAULT_COMPOSER_MODEL`); `tests/test_no_model_literals.py` enforces it.

## Deliverables
1. `crawdad/crawdad/llm/base.py`: `Completion` shape (mirroring
   creek-tools' normalized result) + `AsyncLLMProvider` Protocol with an
   async `complete(messages, *, model, max_tokens) -> Completion`.
2. `crawdad/crawdad/llm/anthropic.py`: `AnthropicAsyncProvider` wrapping
   `AsyncAnthropic`, holding the `messages.create` shape + the
   `AnthropicError` → `RuntimeError(type(exc).__name__)` redaction that
   currently lives in router/composer.
3. `crawdad/crawdad/llm/__init__.py`: `build_async_provider(config)`
   factory (Anthropic only for now).
4. `cli.py`: build the provider via the factory; inject it into router +
   composer.
5. `router.py` / `composer.py`: call `self._provider.complete(...)` instead
   of the raw SDK; drop the direct `anthropic` import + error catch.

## Constraints
- **Zero behavior change.** Same model selection, same error replies, same
  redaction. No new provider yet.
- Do not import anything from `creek-tools`.
- Update existing async tests to the provider seam (mock the provider, not
  the raw SDK) without changing asserted behavior.

## Acceptance
- `./scripts/check-all.sh` exit 0; router/composer tests green.
- `router.py` / `composer.py` no longer reference `anthropic` directly.
