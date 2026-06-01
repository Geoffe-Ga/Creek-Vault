FEAT-040.1: Detector framework, distance-to-voice scoring, and calibrate-against-own-writing harness

## Context
Backbone for FEAT-040. Establishes the typed catalog, the scan engine, the **distance** score model (draft profile vs the user's voice fingerprint), the config, and a calibration harness whose negative set is *the user's own writing*. Every later detector registers into this framework; the framework consumes the `VoiceFingerprint` produced by issue 02.

## Scope
- `tells.py` — typed `Tell`: `id`, `category: Literal["mechanical","lexical","rhetorical","discourse","citation"]`, `feature_key` (the measurable feature this tell corresponds to in the fingerprint, e.g. `"em_dash_density"`, `"ai_vocab.tapestry"`, `"rule_of_three_rate"`), `handling: Literal["autofix","prevent","surface"]`, `description`, `measure(text) -> float` (the draft's rate for this feature), and `detect(text, *, fingerprint, config, context) -> list[Span]`. A `TELL_REGISTRY` with `register()`.
- `model.py` — `Finding` (tell id, span, line, excerpt, `draft_rate`, `user_rate`, `direction: "over"|"under"`) and `ScanReport` (findings, per-feature draft-vs-user deltas, and a scalar `voice_distance`).
- **Distance score** (the crux): `voice_distance(draft_profile, fingerprint, weights)` = weighted, length-normalised divergence between the draft's measured feature vector and the user's. Penalise **both** over-use (draft rate ≫ user rate on avoid-features) and under-use (draft rate ≪ user rate on signature features). A feature where draft ≈ user contributes ~0 regardless of its generic "AI-ness." Weights live in config.
- `scanner.py` — `scan(text, *, fingerprint, config, context) -> ScanReport`; each tell fires only when the draft's `measure()` diverges from the fingerprint's stored rate for that `feature_key` beyond a configurable margin. **If the vault has no measurement for a feature** (sparse corpus), fall back to a conservative generic band (documented) rather than flagging aggressively.
- `AIStyleConfig` in `creek/config.py`: per-feature weights, divergence margins, `voice_distance_upper` threshold, category toggles, `min_fingerprint_fragments` (below which the system warns the fingerprint is unreliable and softens flagging), `enabled`.
- **Calibration harness** `creek/generate/ai_style/calibration/`: instead of a hand-labelled fixture, the negative set is the **user's own authored fragments** (via the 02 profiler's authorship filter) and the positive set is the guide's quoted AI examples (shipped as data). `calibrate(vault) -> CalibrationReport` reports: false-positive rate on the user's real writing (must be near zero — this is the headline metric), detection rate on the AI examples, and per-feature deltas. Mirror `creek/classify/calibration.py` + `DEFAULT_FLOORS`.

Seed with one trivial tell (literal `2025-xx-xx`) to prove the engine.

## Out of scope
The fingerprint computation itself (02). Any repair, prompt injection, guard, lint (later). Real catalogs (03–07).

## Design constraints
- Score is **length-normalised** and **vault-relative**; one stray word must never dominate.
- Graceful degradation when the fingerprint is thin (small/new vault): soften toward the generic prior, and surface a "fingerprint based on only N fragments" caveat rather than over-flagging.
- Pure/deterministic/no-network.

## Files to touch
- new: `creek/generate/ai_style/{__init__,tells,model,scanner}.py`, `calibration/*`
- edit: `creek/config.py` (`AIStyleConfig`), `generate_default_config`
- tests: `tests/generate/ai_style/test_scanner.py`, `test_distance.py`, `test_calibration.py`

## Acceptance criteria
- `voice_distance` is ~0 when a draft's feature vector equals the fingerprint, rises with divergence in either direction, and is documented.
- `calibrate()` reports near-zero false positives on the user's own fragments and non-zero detection on the AI examples; fails loudly if the user's writing flags above a configurable floor.
- Thin-fingerprint fallback exercised by a test. check-all.sh green; ≥90% cov.

## Est. LOC
~550–700. Depends on: 02 (interface; can be co-developed, but 02's `VoiceFingerprint` type should land first or be stubbed). Blocks: 03–10.
