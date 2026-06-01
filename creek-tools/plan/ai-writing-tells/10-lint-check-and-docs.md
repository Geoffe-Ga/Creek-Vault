FEAT-040.10: Voice-fidelity lint check, CLI wiring, and docs

## Context
The audit surface, mirroring `creek/lint/checks/draft_grounding.py`: surface drafts whose voice distance / divergences exceed thresholds, reading stamped frontmatter where present (from 09) and re-scanning otherwise. Framed as "this diverges from your voice," not "this looks like AI." Detection-and-surface only — lint never rewrites.

## Scope
- `creek/lint/checks/voice_fidelity.py` `run(vault_path, since=None) -> CheckResult`:
  - Load the `VoiceFingerprint` (02). Walk `07-Voice/Drafts/` (and, with a `--full`/config flag, other generated surfaces); read `voice_distance`/`voice_findings` frontmatter; for files lacking it, re-run `scan()` against the fingerprint.
  - Emit one finding per file over `voice_distance_upper`, listing the top divergences (over- and under-used features) + remediation hint ("re-run `creek draft` / revise toward your voice"). Skip empty/old files silently. Never modify files. If no fingerprint exists, emit a single informational finding ("run the profiler first") rather than failing.
- Register `"voice-fidelity"` in `runner.py` `DETERMINISTIC_CHECKS` + `_REGISTRY`. Ensure `creek lint` runs it by default and `creek lint --check voice-fidelity` selects it.
- Docs: update `docs/generation.md` (the fingerprint, the guard, how distance is stamped, the authorship filter) and `docs/lint.md` (the check + thresholds). Both MUST carry the epistemics: these are **probabilistic, vault-relative signals, not proof of AI**; the point is matching the user's measured voice, and humans/tools are poor at AI detection — surface for review, never auto-accuse.

## Out of scope
The guard/rewrite (09), detectors (05–07), sanitizers (03–04), fingerprint (02).

## Design constraints
- Mirror `draft_grounding.py`: silent skip / re-scan of unstamped files, no mutation (assert byte-identical after run).
- Docs frame everything as voice-fidelity (vault-relative), and explicitly warn against using the check as an AI-accusation tool.

## Files to touch
- new: `creek/lint/checks/voice_fidelity.py`
- edit: `creek/lint/runner.py`, `creek/lint/__init__.py` (if re-exporting), `docs/generation.md`, `docs/lint.md`
- tests: `tests/lint/test_voice_fidelity_check.py` (stamped + unstamped fixtures, threshold, no-mutation, missing-fingerprint path), runner registration test

## Acceptance criteria
- Surfaces a high-distance draft, silent on an on-voice one; unstamped draft re-scanned; missing fingerprint → informational, not error; never mutates files.
- Docs updated with thresholds + the probabilistic/vault-relative caveat. check-all.sh green; ≥90% cov.

## Est. LOC
~400–600. Depends on: 01, 02 (and 09 for the stamped path; re-scan-only can ship earlier).
