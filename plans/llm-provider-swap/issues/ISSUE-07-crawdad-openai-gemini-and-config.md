# ISSUE #7 — CrawDad OpenAI + Gemini providers + config selection

**Epic:** Swappable LLM providers · **Sequence:** 7/8 · **Risk:** medium–high · **Depends:** #6

## Role
Python engineer in `crawdad/` (async), security lens, max-quality bar.

## Goal
Make CrawDad's backend selectable via `CRAWDAD_PROVIDER`, with OpenAI and
Gemini async providers alongside Anthropic, keys discovered from env.

## Context
- After #6, `crawdad/crawdad/llm/` holds the `AsyncLLMProvider` Protocol,
  an Anthropic impl, and `build_async_provider(config)`.
- `crawdad/crawdad/config.py` today: eagerly requires `ANTHROPIC_API_KEY`,
  stores it on the frozen `CrawDadConfig`, and defines
  `DEFAULT_ROUTER_MODEL` / `DEFAULT_COMPOSER_MODEL` (env-overridable) — the
  only model literals in the package, enforced by
  `tests/test_no_model_literals.py`.
- `load_config` merges `crawdad.yaml` + env secrets.

## Deliverables
1. `config.py`:
   - Add `llm_provider: str` from `CRAWDAD_PROVIDER` (default `anthropic`).
   - Replace the unconditional `ANTHROPIC_API_KEY` check with a
     **provider-conditional** one: require the env key matching
     `llm_provider` (`ANTHROPIC_API_KEY` / `OPENAI_API_KEY` /
     `GOOGLE_API_KEY`), with a clear error naming the missing var.
   - **Stop storing the key on `CrawDadConfig`** — let the SDK read it
     from env (removes a secret from a frozen, widely-passed object).
   - Per-provider router/composer model defaults (Haiku-tier vs
     Sonnet-tier mapping for OpenAI/Gemini), all confined to `config.py`,
     env-overridable as today.
2. `crawdad/crawdad/llm/openai.py`, `gemini.py`: async providers mirroring
   the sync ones from creek-tools #4/#5 (same response/usage/error-redaction
   mapping; async SDK clients reading env keys).
3. `build_async_provider`: register `openai` + `gemini`.
4. `cli.py`: already provider-agnostic after #6 — verify it passes
   `config.llm_provider` through.
5. `pyproject.toml`: add `openai` + `google-genai` (match the package's
   dependency style — direct or optional extras). Regenerate the lock if
   present; update dev/test installs so mocks import.
6. `tests/test_no_model_literals.py`: extend to forbid `gpt-*` /
   `gemini-*` literals outside `config.py`.

## Constraints
- Keys only from env; never stored on config, never logged.
- Allowlists, MCP wiring, agent-loop knobs unchanged.
- Async-mock the three SDKs; no live calls.
- Complexity ≤10; coverage ≥90%; mypy strict.

## Acceptance
- `./scripts/check-all.sh` exit 0.
- `CRAWDAD_PROVIDER=openai` with `OPENAI_API_KEY` set boots the bot's agent
  loop against (mocked) OpenAI; same for `gemini`; default stays Anthropic.
- Missing the provider's key fails fast with a message naming the var.
