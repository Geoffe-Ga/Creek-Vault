# FEAT-018: Replace compost phrase-matcher with embedding-similarity + LLM verification

**Severity:** High (v1.0 quality)
**Category:** FEAT
**Estimated LOC:** ~350 (net — replacing existing 523-line module; estimated +200 / -150)
**Estimated complexity:** M
**Source candidate:** Slop critique surfaced after the comparative analysis (PR #199). The user explicitly chose to keep compost as an LLM-driven feature rather than retire it to manual-only.
**Dependencies:** FEAT-017 (embedding-confidence and tier-safety conventions land first), FEAT-001 (paradox.SKILL.md to ensure compost detection doesn't accidentally flatten paradoxes)
**Parallelizable with peers:** yes (with FEAT-019 and most of Wave 2+)
**Wave:** Wave 2 (post-FEAT-017)

## Goal

`creek/generate/compost.py` is 523 lines of code that triggers a "compost" classification when a fragment title contains one of five hardcoded phrases (`"gave up on"`, `"abandoned"`, `"shelved"`, and two more around lines 32–37). This is metaphor-driven coding: the spec calls compost "decomposing ideas that may fertilize future growth" and the implementation is a regex over five strings against the title only. Replace with an embedding-similarity gate plus an LLM verification step so compost detection has actual signal.

## Files to touch

- `creek-tools/creek/generate/compost.py` — replace `_ABANDONMENT_KEYWORDS` (lines 32–37) and `_matched_abandonment_keywords` with the embedding + LLM verification pipeline.
- `creek-tools/creek/generate/exemplars/compost.yaml` (new) — 10–20 hand-curated example fragments that genuinely express idea-abandonment, written by the user and reviewed before merging. Different in tone from the regex phrases: should cover "I keep circling this and never landing", "this thread went somewhere I no longer recognize", "I let this one go", etc., not just literal "gave up".
- `creek-tools/creek/classify/embeddings_helper.py` (or reuse existing `creek/link/embeddings.py`) — provide `compute_similarity(fragment_body, exemplars)` returning a max-similarity score.
- `creek-tools/tests/fixtures/compost/` (new) — fixture set: 10 positive examples (real abandonment), 10 negatives (false-positive risks like "I was about to give up but then…").
- `creek-tools/tests/test_compost.py` — regression tests covering embedding gate + LLM verification + provenance.
- `creek-tools/docs/emergence.md` — update §10.4 documentation to describe the new pipeline.

## Pre-decided choices

- **Two-stage pipeline:**
  1. **Embedding gate (cheap):** compute cosine similarity between the fragment body (not just title) and each exemplar in `compost.yaml`. If max similarity ≥ `CompostConfig.embedding_threshold` (default 0.72 — looser than the synchronicity 0.9 because compost is more diffuse), proceed to stage 2. Otherwise skip — this fragment is not a compost candidate.
  2. **LLM verification (selective):** invoke a single-fragment, two-state prompt: "Is this fragment expressing abandonment of an idea, a project, or a line of inquiry? Reply `yes`, `no`, or `ambiguous` plus a one-sentence reasoning trace." Only the embedding-gated fragments hit the LLM, so cost stays bounded. `yes` → compost. `ambiguous` → routed to `10-Liminal/Compost/Review/` (manual review queue, similar to existing review queue patterns). `no` → not compost.
- **Title-only matching is removed.** The body is the signal; titles in this corpus are LLM-generated summaries and frequently miss the abandonment signal.
- **Privacy-tier respect:** `intimate`-tier fragments are still subject to compost detection (the user's most intimate journals are often where abandonment lives), but the compost note's body is title-only summary by default (matches the existing privacy_filter behaviour for intimate content).
- **Paradox-aware:** if a fragment is in `10-Liminal/Paradoxes/` or carries `tags: [paradox]`, compost detection is skipped. A paradox-tagged fragment that *also* names abandonment is a paradox first; compost can be added manually by the user if they decide.
- **LOC budget:** replacing 523 lines with ~300 means net deletion. The new module is smaller because removing the hardcoded phrase machinery + the special-case logic around it is the bulk of the deletion. Embedding + LLM call is short.
- **Backwards-compat:** existing compost notes (created by the regex pipeline before this FEAT lands) are kept untouched. The new pipeline runs only on fragments that haven't been compost-classified previously.
- **CompostConfig knobs:**
  ```yaml
  compost:
    embedding_threshold: 0.72
    llm_verification: true       # set false to skip stage 2 (then ambiguous → not compost)
    review_queue_dir: "10-Liminal/Compost/Review/"
  ```

## Test plan

- Unit: embedding gate against fixture positives returns similarity ≥ threshold; against negatives returns < threshold.
- Unit: LLM verification correctly routes `yes`/`no`/`ambiguous` to the right destination (compost / skip / review queue), using a recorded LLM-response fixture (no live calls in unit tests).
- Unit: paradox-tagged fragments skip compost detection.
- Unit: intimate-tier fragments produce title-only compost notes.
- Regression (against the existing keyword cases): fragments with title "I gave up on the X project" should still be detected as compost (assuming the body also expresses abandonment) — verifies we didn't regress on cases the regex caught for the right reason.
- Regression (false-positive removal): fragments with title "I was about to give up but I kept going" should NOT be compost — verifies we removed cases the regex caught wrongly.
- Integration: `creek lint --check compost` (or `creek report --type compost`) against a fixture vault writes the expected compost notes and review-queue entries.
- Coverage: ≥90% branch coverage on the new `creek/generate/compost.py`.

## Acceptance criteria

- Hardcoded `_ABANDONMENT_KEYWORDS` is deleted; replaced with an embedding-similarity gate against `creek/generate/exemplars/compost.yaml`.
- LLM verification step exists and is invoked only on embedding-gated candidates.
- Exemplar set `compost.yaml` has ≥10 entries reviewed by the user.
- A fixture-based test suite covers positive, negative, and false-positive-risk cases.
- Paradox-tagged fragments are skipped by compost detection (verified by regression test).
- The `CompostConfig` schema is exposed in `00-Creek-Meta/creek_config.yaml` with documented defaults.
- `docs/emergence.md` §10.4 documents the new pipeline; the spec at `00-Creek-Meta/Ontology/creek_ontology_agent_prompt.md:996-1003` is unchanged (the spec doesn't prescribe regex; the implementation chose regex).
- ≥90% branch coverage on `creek/generate/compost.py`.

## References

- `creek/generate/compost.py:32-37` (the five hardcoded phrases).
- `creek/generate/compost.py:114-126` (`_matched_abandonment_keywords`).
- Spec §10.4 (compost framing: "decomposing ideas that may fertilize future growth").
- FEAT-017 (sets the few-shot + tier-safety conventions this FEAT mirrors).
- INC-018 (existing tracker for "paradox / tag garden / compost emergence" — this FEAT specifically addresses the compost slice).
