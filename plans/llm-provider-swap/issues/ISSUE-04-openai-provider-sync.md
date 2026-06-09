# ISSUE #4 — `OpenAIProvider` (sync) + optional dependency + tests (creek-tools)

**Epic:** Swappable LLM providers · **Sequence:** 4/8 · **Risk:** medium · **Depends:** #2, #3

## Role
Python library engineer in `creek-tools/`, max-quality bar, `./scripts/*`.

## Goal
Add a synchronous OpenAI provider so `provider: openai` in
`creek_config.yaml` routes classification + author-desk calls to OpenAI,
with the key read from `OPENAI_API_KEY` by the SDK.

## Context
- After #2, `build_provider` is a registry and `LLMProvider` is the
  contract returning `Completion(text, stop_reason, usage)`.
- After #3, cloud providers call the shared consent gate.
- `anthropic` is an **optional extra** in `pyproject.toml`; tests mock the
  SDK (see `tests/test_classify.py`). `[dev]` pulls in `[all]`.
- OpenAI response shape: `resp.choices[0].message.content`;
  `resp.choices[0].finish_reason` (`"stop"` / `"length"` → map to
  `"end_turn"` / `"max_tokens"`); `resp.usage.prompt_tokens` /
  `completion_tokens` (map into the `usage` dict).

## Deliverables
1. `creek/classify/llm/providers/openai.py`: `OpenAIProvider(LLMProvider)`:
   - `__init__`: shared consent gate; require `OPENAI_API_KEY` present
     (else `RuntimeError`); lazy client (`openai.OpenAI()` — reads key +
     optional `OPENAI_BASE_URL` / `config.api_base` from env/config, never
     a stored secret).
   - `DEFAULT_MODEL` (a literal, lives only here); model resolution
     mirrors the Anthropic sentinel logic.
   - `complete(...)`: call `chat.completions.create`; map errors via
     `RuntimeError(type(exc).__name__)`; normalize to `Completion`.
2. Register `"openai"` in the factory.
3. `config.py`: add optional `api_base: str | None = None` to `LLMConfig`;
   extend `provider` docstring.
4. `pyproject.toml`: `[openai]` optional extra (`openai>=1.0`); add to
   `[all]`; regenerate `uv.lock`; update `requirements*.txt`.
5. Tests mocking the `openai` SDK: env-missing → `RuntimeError`; consent
   enforced; response → `Completion`; `finish_reason` mapping; usage
   mapping; error redaction (no request state in the message).

## Constraints
- Key only from env; never logged; never on a config field.
- Mirror the Anthropic provider's structure so the two stay legible.
- Complexity ≤10/function; coverage ≥90% for the new file.

## Acceptance
- `./scripts/check-all.sh` exit 0.
- With `OPENAI_API_KEY` + consent set and `provider: openai`, a mocked
  end-to-end classify call returns a populated `Completion`.
