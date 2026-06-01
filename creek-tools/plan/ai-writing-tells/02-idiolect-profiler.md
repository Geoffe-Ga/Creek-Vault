FEAT-040.2: Idiolect / voice-fingerprint profiler — measure how *this* user actually writes

## Context
The heart of the reframe (FEAT-040). Produces the per-vault `VoiceFingerprint`: a quantitative measurement of the user's real writing habits, used as the false-positive authority and the distance baseline for every detector, and as the source of the idiolect prompt preamble. Complements the qualitative exemplars already harvested by the Voice Skill Tree.

## Scope
- `creek/generate/ai_style/fingerprint.py`:
  - `VoiceFingerprint` (typed, serialisable): for each measurable feature, the user's rate/distribution — AI-vocab per-word rates (so we know which "AI words" the user genuinely uses), em-dash density, curly-quote/apostrophe usage, copula (`is`/`are`/`has`) vs marketing-verb (`serves as`/`boasts`/`features`) ratio, sentence-length distribution (mean/spread), paragraph length, transition-opener rate (`Additionally,`…), rule-of-three frequency, negative-parallelism rate, heading-case habit (sentence vs title), hedging/disclaimer rate, and overall lexical-diversity/type-token stats. Store sample sizes + confidence per feature.
  - `build_fingerprint(vault_path, config) -> VoiceFingerprint` and persistence to `00-Creek-Meta/voice-fingerprint.json` (refreshable; versioned).
- **Authorship filter (critical correctness requirement):** include only genuinely user-authored text. Concretely: `source.author == self`; for `platform in {chatgpt, claude}` conversation fragments use **only the user-turn portion** (or exclude them if the body cannot be split cleanly), since the assistant half is AI text that would poison the baseline; weight `journal`/`markdown` and `substack` highest. Make the inclusion rule explicit and configurable.
- Confidence/thresholding: expose `fragment_count` and per-feature support so issue 01 can soften flagging when the corpus is thin.

## Out of scope
Detection, scoring, repair, prompts (other issues). The qualitative exemplar harvest (already done by `creek skills generate` / `creek report --type voice`).

## Design constraints
- **Do not fingerprint AI text.** A test must prove that assistant-turn content from a ChatGPT export does not enter the fingerprint (e.g. construct a fragment whose user-turn says "I reckon" and assistant-turn is full of "tapestry/underscore", and assert the AI-vocab rates reflect only the user-turn).
- Deterministic, offline, frontmatter-safe (read bodies only).
- Refresh semantics: re-running after more ingestion updates the fingerprint; cheap enough to run at the end of `creek process`/`creek classify` or on demand (`creek report --type voice` is a natural CLI home — wire as a sub-report or a `--fingerprint` flag).
- Privacy tier: honour the same `--include-tier` policy as the rest of the pipeline.

## Files to touch
- new: `creek/generate/ai_style/fingerprint.py`
- edit: a CLI surface to (re)build it — extend `creek report --type voice` or add `creek report --type fingerprint`; `AIStyleConfig` (authorship weights, persistence path)
- tests: `tests/generate/ai_style/test_fingerprint.py` (authorship-filter correctness is the headline test; plus rate computations and persistence round-trip)

## Acceptance criteria
- `build_fingerprint` over a fixture vault yields rates that match hand-computed expectations and **excludes assistant-turn text** (proven by test).
- Fingerprint persists/loads round-trip; carries `fragment_count` and per-feature support.
- Thin-corpus case produces low-confidence flags consumed correctly by 01.
- check-all.sh green; ≥90% cov.

## Est. LOC
~500–700. Depends on: none (defines the `VoiceFingerprint` type 01 consumes). Co-develop with 01; land the type first.
