# FEAT-017: Classification prompt hardening — few-shot, CoT, calibration, default-unclassified bias

**Severity:** High (v1.0 — voice fidelity)
**Category:** FEAT
**Estimated LOC:** ~600 (code ~400 + fixtures ~200; may split into 017a/017b if code crosses 500)
**Estimated complexity:** L (right at the budget; flagged splittable)
**Source candidate:** This FEAT addresses a "slop signal" critique surfaced after the comparative analysis merged. There is no candidate file — the critique is the source. See PR #199 context + the slop critique in the project history.
**Dependencies:** INC-019 (done), FEAT-001/002 (paradox + wavelength-aware skill files define the contract the prompt should honour)
**Parallelizable with peers:** yes (with FEAT-018 and FEAT-019; FEAT-004 should land *after* this to consume hardened classifications)
**Wave:** late Wave 1 / early Wave 2 (before any consumer of LLM classification — at minimum FEAT-004, FEAT-014, FEAT-015)

## Goal

The current `creek/classify/llm.py:54` prompt asks a (default) local Ollama model to pick a 7-dimensional tuple from 94,500 combinations in one shot with no examples, no chain-of-thought, no calibration, and no human-in-the-loop sample check. Downstream consumers — voice-skill activation, `creek draft`, compile-time synthesis, the audit report's wavelength snapshot — treat the YAML response as truth. Voice fidelity is downstream of classification quality; this FEAT closes that gap.

## Files to touch

- `creek-tools/creek/classify/llm.py` — replace the single-shot prompt with a two-step (reason → emit) pipeline; load few-shot examples from fixture; add per-dimension confidence threshold gates.
- `creek-tools/creek/classify/examples/` (new directory) — markdown/YAML fixtures: 5–10 hand-curated example fragments per dimension covering Medicine/Toxic, ambiguous phase, multi-frequency content, paradox indicators.
- `creek-tools/creek/classify/calibration.py` (new) — runs the LLM against a held-out labeled fixture set and reports per-dimension agreement rates.
- `creek-tools/tests/fixtures/classification/calibration_set.yaml` (new) — ~50 hand-labeled fragments (Geoff labels them; this FEAT establishes the format and ships a starter set; full set lands incrementally).
- `creek-tools/tests/test_calibration.py` (new) — CI test that runs calibration and asserts per-dimension agreement ≥ a documented floor (start lenient: ≥40% for Mode/Orientation/Dosage, ≥60% for Frequency/Phase, ≥75% for Voice Register).
- `creek-tools/creek/classify/classify_engine.py` — wire the two-step pipeline through `--method llm`; default-unclassified bias for low-confidence dimensions.
- `creek-tools/creek/cli.py` — add `creek classify --calibrate` subcommand that runs the calibration fixture and prints per-dimension agreement.
- `creek-tools/docs/classification.md` — document few-shot examples, CoT pipeline, calibration model floor, and per-dimension default-unclassified thresholds.

## Pre-decided choices

- **Two-step pipeline (CoT):** the prompt now asks the model first for a brief reasoning trace ("Walk through which frequency this most resonates with and why, then which phase, then which mode..."), then for the YAML tuple. Reasoning trace is captured in `classification.reasoning` frontmatter for auditability (truncated to 400 chars to stay tier-safe — intimate-tier fragments don't leak reasoning into vault).
- **Few-shot format:** 5–10 examples per dimension, drawn from already-classified fragments where the user has reviewed and accepted the classification. Stored as YAML lists under `creek/classify/examples/{frequency,phase,mode,dosage,register,confidence}.yaml`. Loaded into the prompt as a rotating sample (3–5 per call to keep token cost bounded) using a stable hash of the fragment ID as the seed (deterministic per fragment, varied across the corpus).
- **Default-unclassified bias:** for **Mode**, **Orientation**, and **Dosage** specifically, if the model's self-reported confidence < `LLMConfig.unclassified_threshold` (default 0.55), the dimension defaults to `unclassified` rather than the model's pick. Frequency, Phase, and Voice Register keep the model's pick — these are more stable signals.
- **Calibration model floor:** Haiku (`claude-haiku-4-5-20251001`) and mistral (default Ollama) are too small for reliable 7-dimensional one-shot picks. The calibration test documents the expected accuracy floor at each tier. The prompt itself is model-agnostic; recommended model is Sonnet (`claude-sonnet-4-6`) for production classification. Document this clearly so users don't run mistral and trust the output.
- **Calibration cadence:** the CI test runs the calibration set on every PR that touches `creek/classify/`. Locally, `creek classify --calibrate` is a separate subcommand the user invokes when tweaking the prompt or the example set.
- **Reasoning trace tier-safety:** reasoning is logged to vault frontmatter for `open` and `personal` tiers (truncated to 400 chars); for `intimate` tier, reasoning is logged *only* to `00-Creek-Meta/Processing-Log/classify-llm-trace.jsonl` (gitignored per FEAT-019), never into the fragment frontmatter.
- **Per-fragment classification is acknowledged as noisy.** Documentation explicitly says the wavelength signal is reliable as a *weekly aggregation across many fragments*, not as a single-fragment certainty. The audit report's wavelength snapshot (FEAT-006/007, merged) already aggregates; this FEAT just makes the inherent uncertainty per-fragment explicit in docs.
- **Default privacy tier of `classification.reasoning` field:** inherits from the fragment's `privacy_tier`. Never escalated.
- **Splittability:** if the implementing PR exceeds 500 LOC of non-fixture code, split into FEAT-017a (prompt + CoT pipeline in `llm.py`) and FEAT-017b (calibration fixture + CI gate). FEAT-018 / FEAT-019 do not depend on which side lands first.

## Test plan

- Unit: `_build_few_shot_prompt(fragment, examples)` produces a stable prompt for a stable input (deterministic example sampling).
- Unit: the two-step pipeline call captures reasoning trace + parsed YAML; reasoning trace is truncated to 400 chars in fragment frontmatter and stored in full in the trace log.
- Unit: the default-unclassified bias activates when model confidence < threshold for Mode/Orientation/Dosage; does NOT activate for Frequency/Phase/Voice Register.
- Unit: intimate-tier fragments have empty `classification.reasoning` in their frontmatter; the trace log captures the full reasoning.
- Integration: `creek classify --calibrate` runs the fixture set, reports per-dimension agreement, exits 0 if all floors met.
- CI: a `tests/test_calibration.py` job runs the calibration set every PR that touches `creek/classify/`; floor breach fails the build.
- Regression: existing `creek classify --method llm` callers continue to work; the new pipeline is the only path for new classifications, but accepts the old YAML format for backwards compatibility.
- Documentation: `docs/classification.md` documents the new pipeline, the model floor, and the per-fragment-is-noisy / weekly-aggregation-is-signal framing.

## Acceptance criteria

- `CLASSIFICATION_PROMPT` in `creek/classify/llm.py` includes 3–5 rotating few-shot examples per dimension drawn from `creek/classify/examples/`.
- The prompt is a two-step (reasoning → YAML) pipeline; reasoning trace is captured and tier-routed correctly.
- Default-unclassified bias is implemented for Mode/Orientation/Dosage with a configurable threshold (default 0.55).
- A calibration fixture exists at `tests/fixtures/classification/calibration_set.yaml` with ≥30 hand-labeled fragments (full ~50 lands incrementally via additional fragments added by the human reviewer).
- A `creek classify --calibrate` CLI subcommand exists and reports per-dimension agreement.
- The CI test fails when calibration agreement drops below the documented per-dimension floor.
- `docs/classification.md` documents the new pipeline, the model floor recommendation (Sonnet), the per-fragment-noisy / aggregate-meaningful framing, and the default-unclassified thresholds.
- ≥90% branch coverage on the changed `creek/classify/` paths.

## References

- The slop critique (no public artifact; documented in PR conversation history).
- `creek/classify/llm.py:54-96` (the prompt as it stands today).
- INTEGRATION-PLAN.md "Voice fidelity through the stack" — this FEAT is the gate at the input layer.
- ADOPT-006 / FEAT-006 (confidence tiers on edges — analogous discipline at a different layer).
- Spec sections relevant to dimensional ambiguity: §6.2 ("Don't force it. If a fragment doesn't clearly map to any Frequency, leave it `unclassified`"), §7.4 (Medicine vs Toxic ambiguity), §10.2 (paradox preservation).
