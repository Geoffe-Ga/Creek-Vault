# ISSUE #8 — Docs + ADR: env-var matrix and the decoupling decision

**Epic:** Swappable LLM providers · **Sequence:** 8/8 · **Risk:** low · **Depends:** #5, #7

## Role
Technical writer / engineer across both packages, max-quality bar.

## Goal
Document how to select a provider and supply its key from the
environment, and record the architectural decision to keep the two
provider abstractions decoupled.

## Context
- creek-tools selects via `creek_config.yaml::llm.provider`
  (`ollama` | `anthropic` | `openai` | `gemini`) + optional `api_base`.
- CrawDad selects via `CRAWDAD_PROVIDER` env var.
- Consent: `CREEK_CLOUD_CONSENT` (legacy `CREEK_ANTHROPIC_CONSENT` alias)
  gates any cloud egress in creek-tools.

## Deliverables
1. `creek-tools/README.md`: a provider/env-var matrix:

   | Provider | `llm.provider` | Env key | Consent |
   |---|---|---|---|
   | Ollama (local) | `ollama` | — | not required |
   | Anthropic | `anthropic` | `ANTHROPIC_API_KEY` | `CREEK_CLOUD_CONSENT=1` |
   | OpenAI | `openai` | `OPENAI_API_KEY` (+ opt. `OPENAI_BASE_URL`) | `CREEK_CLOUD_CONSENT=1` |
   | Gemini | `gemini` | `GOOGLE_API_KEY` / `GEMINI_API_KEY` | `CREEK_CLOUD_CONSENT=1` |

   State plainly: keys are read from the environment by the SDK and must
   never be written into `creek_config.yaml` or the repo.
2. `crawdad/README.md` + `crawdad/CLAUDE.md` §5: document
   `CRAWDAD_PROVIDER` and the provider-conditional key requirement
   (`ANTHROPIC_API_KEY` / `OPENAI_API_KEY` / `GOOGLE_API_KEY`).
3. `creek-tools/CLAUDE.md`: note the optional `[openai]` / `[gemini]`
   extras and that `[all]`/`[dev]` include them for test collection.
4. ADR (`creek-tools/docs/architecture/ADR/` and/or `crawdad/docs/adr/`):
   record "two parallel provider abstractions, no shared package" — the
   sibling boundary, the accepted ~30–40-line duplication, the sync/async
   rationale, and the trigger to revisit (duplicated bodies growing real
   logic).

## Constraints
- DRY docs: link, don't duplicate, between READMEs and CLAUDE.md.
- No secret material in any example — use placeholder var names only.

## Acceptance
- Both READMEs show the matrix; the ADR is committed; docstring/interrogate
  gates green. A new operator can pick a provider and supply its key from
  env using only the docs.
