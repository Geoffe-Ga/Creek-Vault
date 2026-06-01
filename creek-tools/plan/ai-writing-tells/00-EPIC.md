FEAT-040 (epic): Make generated text sound like *this user*, by measuring their voice from the vault

## Summary
The goal is not "make output undetectable as AI." It is: **make everything the generation pipeline emits sound like the specific person whose writing is in this vault.** AI has a strong, recognisable accent; even with the full Voice Skill Tree and grounded source material (FEAT-032), generic AI tropes still leak into `creek draft` output. This epic measures the user's actual idiolect from their own ingested writing and uses that measurement to (a) steer generation toward their voice and (b) detect and close the gap where the AI accent has crept in.

The Wikipedia field guide *"Signs of AI writing"* (WP:AISIGNS) is the **prior** — the candidate set of features worth looking at. The user's vault is the **posterior** — the authority on which of those features are actually foreign to *this* writer. A "tell" is therefore redefined: it is not "a word on a list," it is **a measurable divergence between the draft and the user's own writing.**

## The reframe (read this first — it changes every child issue)
Previously the design flagged features against a generic human baseline with generic false-positive caveats. That is wrong for our goal. Two consequences:

1. **False-positive caveats are derived from the vault, not hand-written.** If the user demonstrably uses em-dashes, curly quotes, the rule of three, or the word "tapestry" at rate R in their own authored fragments, then the draft using them at ≤ R is *voice*, not a tell — suppress it. The candidate tell list only tells us *what to measure*; the vault tells us *what to flag*.
2. **The score is a distance, not a count.** We score a draft by how far its feature profile sits from the user's measured feature profile (the "voice fingerprint"), in both directions: over-using something the user avoids, AND under-using something the user does (e.g., the user writes short declarative `is`/`has` sentences but the draft is wall-to-wall `serves as`/`stands as`). Closing the gap — not zeroing a checklist — is the objective.

## The one critical data-hygiene constraint
The vault is built by ingesting the user's sources — and several of those are **not the user's own prose**:
- ChatGPT/Claude conversation fragments are *turn pairs*; the assistant half is AI text. Fingerprinting it would poison the baseline with the exact accent we're trying to remove.
- Substack posts are the user's published (often edited) voice — high signal.
- Journals / personal markdown are the purest signal.

The idiolect profiler (issue 02) MUST fingerprint **only genuinely user-authored text**: restrict to `source.author == self`, exclude the assistant side of conversations (use the user-turn only, or down-weight `platform in {chatgpt, claude}`), and weight journal/markdown + substack highest. Document the authorship filter explicitly; getting this wrong silently defeats the whole epic.

## How the three layers change
- **Prevention (primary).** The prompt preamble is *data-driven*: it states the user's measured habits as positive targets ("this writer uses plain `is`/`has`, rarely uses abstract `tapestry`/`landscape`, favours short sentences, seldom opens with `Additionally`") alongside the avoids. Steer toward the idiolect, not merely away from AI. Pairs with the existing exemplar-bearing skill tree (qualitative) by adding the quantitative contrast.
- **Repair (narrow, vault-aware).** Only auto-fix features that are *never* the user's voice: stray markup artifacts (`oaicite`, `turn0search`, `grok-card`, `utm_source=`) — always strip. Typography (curly quotes, em-dash density) is auto-normalised **only when the vault fingerprint shows the user doesn't write that way**; if they do, leave it. Mechanical structure fixes (Markdown→target, placeholders) stay.
- **Detection (distance-to-voice).** The guard and lint check report *"this draft diverges from your voice in these ways"*, not *"this looks like AI."* The user's own writing is the negative set: the detector is correct only if it does NOT flag the user's authentic fragments.

## Architecture (mirror the existing grounding guard)
Unchanged from before; the new piece is the fingerprint that calibrates everything:
- `creek/generate/grounding.py` + `draft_grounding.py` + `runner.py` + `DraftConfig` — the guard/lint/config pattern to mirror.
- `creek/generate/drafts.py` — `_compose_prompt()` (inject idiolect preamble), `save_draft()`/`save_outline_draft()` (run the voice-fidelity guard).
- The Voice Skill Tree (`creek skills generate`) + voice profiles (`creek report --type voice`) already harvest *qualitative* exemplars; the idiolect fingerprint adds the *quantitative* layer they lack.
New subsystem: `creek/generate/ai_style/` (fingerprint + detector + sanitizer + guard), an `AIStyleConfig`, an `ai-style`/voice-fidelity lint check, and idiolect-driven prompt injection. The guard stamps `voice_distance` / `voice_findings` into draft frontmatter as grounding stamps `grounding_score`.

## Child issues (each ≤ 700 LOC net incl. tests; dependency order)
1. **01 — Detector framework, distance scoring, calibration-against-own-writing harness.** Typed `Tell`/`Finding` registry; `scan(text, fingerprint, config)`; the **distance** score model (draft profile vs fingerprint, both directions); `AIStyleConfig`; a calibration harness whose negative set is *the user's own authored fragments* (success = near-zero findings on the user's real writing). Consumes the fingerprint from 02.
2. **02 — Idiolect / voice-fingerprint profiler (the heart of the reframe).** Scan only genuinely user-authored vault content (authorship filter above) and emit a persisted `VoiceFingerprint`: measured rates/distributions for every candidate feature (AI-vocab word rates, em-dash & curly-quote density, copula vs `serves as`/`boasts` ratio, sentence-length distribution, transition-opener rate, rule-of-three frequency, heading-case habit, parallelism rate, paragraph length, …). This *is* the per-vault false-positive layer. Refreshable as the vault grows.
3. **03 — Sanitizer: typography (vault-aware) + markup artifacts (always strip).** Strip `oaicite`/`turn0search`/`grok-card`/`utm_source` unconditionally; normalise curly quotes / em-dash density **only when the fingerprint says the user doesn't use them**.
4. **04 — Sanitizer: Markdown→target structure, headings, breaks, placeholders, emoji.** (Mechanical; heading-case target taken from the fingerprint's measured habit.)
5. **05 — Lexical detectors, vault-relative.** AI-vocab/copulative/transition/`concrete` flagged only above the user's measured rate (+ margin).
6. **06 — Rhetorical detectors, vault-relative.** Significance/legacy puffery, superficial `-ing`, peacockery, weasel attribution — suppressed where the fingerprint shows the user writes that way.
7. **07 — Discourse detectors, vault-relative.** Parallelisms, rule-of-three, Challenges/Future sections, cutoff disclaimers, comm boilerplate — density-gated against the user's measured rates.
8. **08 — Idiolect-driven prompt prevention.** Preamble = measured user habits as positive targets + catalog avoids, derived from fingerprint + registry; injected into all prompt paths.
9. **09 — Voice-fidelity guard + revision loop in `creek draft`.** sanitize → measure distance-to-fingerprint → bounded targeted rewrite that moves the draft toward the user's voice (not just removes words) → stamp `voice_distance`/`voice_findings`; keep the lower-distance version; respect grounding floor and `--no-llm`.
10. **10 — `ai-style` / voice-fidelity lint check + docs.** Surface "draft diverges from your voice" from stamped frontmatter / re-scan; register in `runner.py`; docs carry the probabilistic-signs-not-proof caveat and the vault-relative framing.
11. **11 (optional) — Citation-integrity detectors.** Unchanged from prior plan; lowest priority.

## Cross-cutting acceptance criteria (every child)
- TDD; ≥90% branch cov; ≥95% docstring; complexity ≤10; mypy strict; ruff clean; `./scripts/check-all.sh` == 0; no unjustified `# noqa`/`type: ignore`.
- **The decisive test for detectors:** run over the user's *own* authored fragments and assert near-zero findings (their real voice must not flag). Run over the guide's AI examples and assert they do flag. Precision is measured against the user's corpus, not asserted.
- Profiler must isolate user-authored text (authorship filter); a test must prove assistant-turn text does not enter the fingerprint.
- Deterministic sanitizers idempotent, frontmatter-safe, and vault-aware where noted.
- Feature branch per issue; conventional commits; green before review.

## Why this is different from "anti-AI-detector"
Anti-detection asks "could a classifier tell this is AI?" — a moving, adversarial target the guide itself says humans and tools are bad at. Voice-fidelity asks "does this match the distribution of how *this person* writes?" — a stable, measurable target grounded in their own corpus. The second subsumes the useful part of the first (AI tropes the user doesn't share get removed) while also fixing things a pure AI-detector ignores (the user's characteristic rhythms, hedges, and quirks that the model flattens away).
