## Epic Summary

Make drafted essays sound like the owner. Today three independent defects pull drafted prose
away from the owner's real voice, and this epic fixes all three behind a single diagnostic that
makes each one observable:

1. **Audience is ignored.** The voice proxy weights a private chat message the same as a
   published essay. Public-facing work — and the **citation/quotation habit** that is prevalent
   in it but rare in private writing — should carry **more authority** over the stylistic and
   content patterns that shape drafts.
2. **AI text poisons the corpus.** AI chatbot conversations are ingested as a single merged
   fragment defaulting to `source.author = self`, so the AI's prose is treated as a voice
   exemplar. Human turns are the owner's voice; AI turns are not and must be quarantined.
3. **The de-slop pass is not faithful.** The final AI-mannerism guard runs but silently no-ops
   when the fingerprint is thin/unresolved, and only rewrites above a permissive ceiling — so
   mannered drafts get *measured* but not *stripped*.

The epic delivers a graduated audience-authority model, a citation-density voice pattern, correct
per-turn attribution for AI-chat ingests, and a de-slop pass that provably strips tells on the
real `creek draft` path — all proven by a new `creek voice-authenticity` diagnostic.

## Scope

**In scope:**
- A read-only `creek voice-authenticity` diagnostic + typed report (audience mix, AI-corpus leak, de-slop execution status).
- Per-turn attribution in the Claude + ChatGPT ingestors (human → `self`, AI → `ai`, `voice_weight=0.0`).
- Graduated audience-authority weighting in voice exemplar selection / ranking / pattern aggregation, driven by `privacy_tier` and the currently-unused `representativeness`.
- A citation/quotation/reference-density pattern in `VoicePatternExtractor`, weighted by audience and surfaced in the generated voice skill.
- Hardening `DraftGenerator._apply_voice_fidelity` / `run_voice_fidelity_guard`: no silent no-ops, a distinct de-slop **target** threshold, and an integration test proving tells are stripped on the real path.
- Config knobs, existing-vault migration via re-ingest, docs, and an end-to-end regression.

**Out of scope:**
- Changing the ontology classification model beyond attribution/audience fields.
- New mediums or CrawDad surface changes (the composer already delegates drafting to `creek.draft`).
- Re-training or replacing the fingerprint/scanner algorithm itself (we fix faithful *execution*, not the distance metric's internals).
- Raising any `ai-as-user` / borrowed-author `voice_weight` (remains a separate audited opt-in).

## Success Criteria

The epic is done when:

- [ ] `creek voice-authenticity --vault <path>` reports audience mix, AI-corpus-leak fraction, and (given a draft) de-slop execution status.
- [ ] Freshly ingesting an AI chat export yields **separate** fragments: human turns `source.author=self`, AI turns `source.author=ai` with `voice_weight=0.0`; AI turns are provably excluded from the voice corpus.
- [ ] Voice exemplar selection/ranking applies a graduated audience-authority multiplier (`OPEN` > `PERSONAL`; `INTIMATE` still excluded by default) and consumes `representativeness`.
- [ ] The voice proxy measures citation/quotation density and surfaces it in the generated voice skill, weighted by audience.
- [ ] A mannered essay drafted through the real `creek draft` path comes out with the planted tells **removed** (not merely stamped); any de-slop no-op is loudly reported, never silent.
- [ ] Existing vaults migrate via documented re-ingest; the diagnostic's AI-corpus-leak drops accordingly.
- [ ] All child issues closed; `cd creek-tools && ./scripts/check-all.sh` green on `main`.

## Child Issues

_Filled in after child issues are filed._

- [ ] #NNN — Skeleton: `creek voice-authenticity` diagnostic surface
- [ ] #NNN — Core: split AI-chat ingests by turn; attribute human=self / AI=ai
- [ ] #NNN — Core: audience-weighted voice authority
- [ ] #NNN — Core: citation/quotation-density voice pattern (audience-weighted)
- [ ] #NNN — Core: make the de-slop pass faithful and loud on the real draft path
- [ ] #NNN — Edges/Polish: config knobs, migration, docs, end-to-end regression

## Sequencing Notes

- **Internal order:** `ISSUE_01 → 02 → 03 → 04 → 05 → 06`. Issue 01 (skeleton) lands first and
  defines the measurement every other issue makes real. Issue 04 depends on 03 (citation density
  is weighted by 03's audience multiplier). Issue 06 depends on 02–05.
- **Parallel-safe:** 02 and 03 touch disjoint modules (ingest vs. generate/voice) and may run
  concurrently after 01, but 02 is prioritized as a correctness fix.
- **Blocks:** nothing outside this epic.

## Open Decisions

See `2026-06-05_SPEC_summary.md` → "Open decisions": (1) reuse `Authorship.AI` for AI turns vs.
add a distinct `ai-assisted` value (default: reuse `AI`); (2) introduce a `voice_distance_target`
distinct from the `voice_distance_upper` ceiling (default: yes).

## Reference

`plans/essay-voice-authenticity/decomposition/2026-06-05_SPEC_summary.md` — problem statement,
current-state code findings, and sequencing.

## Labels

`epic`, `spec-decomposition`, `voice`
