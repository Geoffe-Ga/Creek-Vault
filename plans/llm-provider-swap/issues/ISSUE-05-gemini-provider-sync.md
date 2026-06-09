# ISSUE #5 — `GeminiProvider` (sync) + optional dependency + tests (creek-tools)

**Epic:** Swappable LLM providers · **Sequence:** 5/8 · **Risk:** medium · **Depends:** #2, #3

## Role
Python library engineer in `creek-tools/`, max-quality bar, `./scripts/*`.

## Goal
Add a synchronous Google Gemini provider so `provider: gemini` routes
classification + author-desk calls to Gemini, key read from
`GOOGLE_API_KEY` (or `GEMINI_API_KEY`).

## Context
- Use the current SDK **`google-genai`** (`from google import genai`),
  **not** the legacy `google-generativeai`. `genai.Client()` reads
  `GOOGLE_API_KEY` / `GEMINI_API_KEY` from env.
- Gemini response shape: `resp.text` (convenience) or
  `resp.candidates[0].content.parts[].text`;
  `resp.candidates[0].finish_reason` (`STOP` / `MAX_TOKENS` → map to
  `"end_turn"` / `"max_tokens"`); `resp.usage_metadata`
  (`prompt_token_count` / `candidates_token_count` → `usage` dict).
- After #2/#3 the `LLMProvider` contract and shared consent gate exist.

## Deliverables
1. `creek/classify/llm/providers/gemini.py`: `GeminiProvider(LLMProvider)`:
   - `__init__`: shared consent gate; require `GOOGLE_API_KEY` **or**
     `GEMINI_API_KEY` present (else `RuntimeError`); lazy `genai.Client()`.
   - `DEFAULT_MODEL` (literal lives only here); model resolution mirrors
     the others.
   - `complete(...)`: `client.models.generate_content(...)`; map errors via
     `RuntimeError(type(exc).__name__)`; normalize to `Completion`
     (max-tokens via `generation_config`).
2. Register `"gemini"` in the factory.
3. `pyproject.toml`: `[gemini]` optional extra (`google-genai>=...`); add
   to `[all]`; regenerate `uv.lock`; update `requirements*.txt`.
4. Tests mocking `google.genai`: env-missing (both vars) → `RuntimeError`;
   consent enforced; response → `Completion`; finish-reason + usage
   mapping; error redaction.

## Constraints
- Key only from env; never logged or stored on config.
- Mirror the Anthropic/OpenAI provider structure.
- Complexity ≤10; coverage ≥90% on the new file.

## Acceptance
- `./scripts/check-all.sh` exit 0.
- `provider: gemini` with a key + consent set routes a mocked classify call
  to a populated `Completion`. **creek-tools is now fully swappable.**
