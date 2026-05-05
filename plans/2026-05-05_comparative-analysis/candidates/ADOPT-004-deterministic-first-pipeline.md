# ADOPT-004: Deterministic-First Pipeline (No LLM Until Necessary)

**Verdict:** ADOPT
**Source system:** Graphify
**Affects:** Creek Vault data layer
**Roadmap target:** v1 (largely already adopted; this is about making it explicit and audit-able)
**Estimated complexity:** S
**Conflicts with non-negotiables?** none

## What it is

Graphify's three-pass architecture: do all deterministic, no-LLM work first (tree-sitter ASTs in Pass 1, faster-whisper transcription in Pass 2), then dispatch only the residue — markdown, PDFs, images, transcripts — to LLM extraction in Pass 3. Only Pass 3 incurs token cost; only Pass 3 leaves the machine. Cited from [`how-it-works.md` v6](https://github.com/safishamsi/graphify/blob/v6/docs/how-it-works.md) and the [v3 architecture doc](https://github.com/safishamsi/graphify/blob/v3/ARCHITECTURE.md).

## Why it's interesting

Two distinct wins from one design choice:
1. **Cost control.** Cheap deterministic work runs always; expensive LLM work runs only on residue. This is what makes Graphify's per-query token claims at all defensible (even if the headline number is corpus-cherry-picked).
2. **Privacy by construction.** The locality boundary is enforced by the pipeline, not by policy. Pass 1 and Pass 2 don't have network access by design.

For Creek Vault, the privacy framing is the more important win. `creek classify --method rules` followed by `creek classify --method llm` already does this in spirit, but the implementation isn't *audit-able* — there's no document that lists "what was deterministic, what was LLM, what residue remains." The Graphify pattern names the discipline.

## Fit with Creek Vault and/or CrawDad

Creek already has most of the pieces:

| Graphify Pass 1 (deterministic) | Creek today |
|---|---|
| Tree-sitter AST extraction | Ingestor parsing per source type, redaction patterns, frontmatter generation |
| Whisper transcription | (none — multimodal scope is narrower; see ADAPT-006) |

| Graphify Pass 3 (LLM) | Creek today |
|---|---|
| Concept extraction from prose | `creek classify --method llm` |
| `semantically_similar_to` edges | `creek link --method embeddings` (different mechanism, same role) |

What's missing is the explicit boundary and the audit trail. Specifically:

- **Pre-LLM yield reporting.** After deterministic ingestion + redaction + rules-classification, what fraction of fragments are fully classified? Creek's docs claim "~70% of fragments confidently" for the rules pass, but nothing reports the actual yield per run.
- **Residue tracking.** A named concept of "what's left for the LLM pass" — fragments that are unclassified or below the confidence threshold. Today this is implicit.
- **No-network-during-Pass-1 enforcement.** Creek's ingestion is local-first by default but the pipeline doesn't *enforce* it the way Graphify does (Pass 1 has no Anthropic SDK in scope). For privacy claims to be credible, the boundary should be load-bearing.

## Translation if adapted

The translation is mostly about *naming* what already exists:

1. Document the pipeline in the same Pass 1 / Pass 2 / Pass 3 vocabulary in the ontology spec or a sibling doc:
   - **Pass 1 (local, deterministic):** ingestion, redaction, rules-based classification, frontmatter generation. No network.
   - **Pass 2 (local, model-based):** embedding generation (sentence-transformers), OCR (pytesseract), Whisper transcription if/when added. Local model inference; no network.
   - **Pass 3 (network if opted-in):** LLM classification of residue, LLM-driven compile, lint semantic checks.
2. After every `creek process` run, emit a "pre-LLM yield" line in the run log: `Deterministic: 7,431 fragments fully classified | Residue: 1,892 unclassified or low-confidence`.
3. Make Pass 3 opt-in *per run*, not just per config. `creek process --no-llm` should run end-to-end deterministically, classify-with-rules-only, and report residue without ever calling Anthropic. This is the privacy-defaulting story made tangible.

## Dependencies

- Pairs with: ADOPT-005 (audit report includes pre-LLM yield numbers as a section), ADAPT-006 (Whisper transcription would be a Pass 2 addition).

## Acceptance criteria

- The ontology docs (or `creek-tools/CLAUDE.md`) document the three-pass pipeline with the explicit privacy claim ("network egress only in Pass 3").
- `creek process` emits a structured run summary that names how much was classified deterministically vs. how much residue went to LLM.
- A `creek process --no-llm` flag exists and runs end-to-end without any Anthropic / Ollama call (tested in CI with network egress denied).
- A regression test verifies that with `--no-llm`, network egress hooks see zero Anthropic-bound traffic during a full pipeline run.
- The "pre-LLM yield" metric is exposed in the audit report (see ADOPT-005).
