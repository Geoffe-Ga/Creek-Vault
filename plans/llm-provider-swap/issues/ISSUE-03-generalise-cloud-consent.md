# ISSUE #3 — Generalise the cloud-egress consent gate (creek-tools)

**Epic:** Swappable LLM providers · **Sequence:** 3/8 · **Risk:** low · **Depends:** #2

## Role
Python engineer in `creek-tools/` with a security lens, max-quality bar.

## Goal
Make the "fragment content is leaving the device" consent gate and
warning **provider-neutral** so they cover OpenAI and Gemini (added
next), not just Anthropic. Ollama (local) stays exempt.

## Context
- `creek/classify/llm/providers.py` (Anthropic) enforces:
  - `ANTHROPIC_API_KEY` present, else `RuntimeError`.
  - `CREEK_ANTHROPIC_CONSENT` in `{"1","true","yes"}`, else `RuntimeError`
    with a message about fragment content reaching Anthropic's servers.
  - `ANTHROPIC_CLOUD_WARNING` constant, logged by the orchestrator.
- This consent semantics applies to ANY cloud provider.

## Deliverables
1. A provider-neutral consent helper (e.g.
   `creek/classify/llm/consent.py`): reads `CREEK_CLOUD_CONSENT`
   (**accept the legacy `CREEK_ANTHROPIC_CONSENT` as an alias** for
   back-compat), same truthy set, raising a message that names the
   **active provider**.
2. A `cloud_warning(provider_name)` helper replacing the static
   `ANTHROPIC_CLOUD_WARNING` (keep the old constant as a deprecated
   alias bound to the Anthropic name).
3. Cloud providers call the shared consent check in `__init__`; Ollama
   does not. The orchestrator logs the neutral warning for any cloud
   provider.

## Constraints
- Back-compat: existing `CREEK_ANTHROPIC_CONSENT=1` setups must keep
  working unchanged. Document the new var name in the message + README
  (README update lands in #8).
- TDD: legacy var still consents; new var consents; neither set →
  `RuntimeError` naming the provider; Ollama path never requires consent.

## Acceptance
- `./scripts/check-all.sh` exit 0. Existing Anthropic consent tests green
  (now via the shared helper). New tests cover the alias + neutral message.
