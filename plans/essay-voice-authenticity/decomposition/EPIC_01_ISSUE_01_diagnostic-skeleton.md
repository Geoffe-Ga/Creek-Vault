## Role

You are a senior Python engineer working in `creek-tools/`, fluent in the `creek` CLI (Typer),
the `Fragment` model, and the FEAT-040 voice pipeline. You build read-only diagnostics that make
hidden state observable.

## Goal

Ship a read-only `creek voice-authenticity` command and a typed `VoiceAuthenticityReport` that
audits a vault's voice corpus and (optionally) a drafted essay, emitting **three sub-scores**.
This is the tracer skeleton: each sub-score is backed by a *minimal but real* probe over existing
data now, and later issues replace each probe with the full implementation. The command must run
end-to-end and print sensible numbers on a real vault from day one.

## Context

- **Parent epic:** #EPIC_01_NUMBER
- **Predecessor issue(s):** none — this is the skeleton.
- **Findings:** `plans/essay-voice-authenticity/decomposition/2026-06-05_SPEC_summary.md`.
- **Files involved:**
  - `creek-tools/creek/generate/` — add `voice_authenticity.py` (report dataclass + probes).
  - `creek-tools/creek/cli.py` — register the `voice-authenticity` Typer command (mirror the existing `voice-check` command at `cli.py:1408`).
  - `creek-tools/tests/` — smoke tests over a fixture vault.
- **The three sub-scores:**
  1. `audience_mix` — distribution of the *voice-eligible* corpus by `privacy_tier`
     (`OPEN` / `PERSONAL` / `INTIMATE`) and a boolean `audience_weighting_active` (stub: `False`
     until Issue 03).
  2. `ai_corpus_leak` — count and fraction of voice-eligible fragments whose
     `source.platform ∈ {claude, chatgpt}` (these are AI-chat ingests; today they leak in — see
     Issue 02). Real today: just filter on platform.
  3. `deslop` — given an optional `--draft <path>`, read the draft's frontmatter and report
     whether the AI-mannerism guard left attestation (`voice_distance` present?), whether it was
     `rewritten` vs only `measured`, and a `status` reason. Stub the `status` string until Issue 05.
- **Prior decisions:** read-only — this command MUST NOT mutate the vault. Reuse the existing
  fragment-loading helper that `voice-check` uses; do not re-implement vault walking.

## Output Format

A single PR containing:

- [ ] `VoiceAuthenticityReport` dataclass (frozen) with the three sub-score structs and a `summary_line()` + `to_json()`.
- [ ] `creek voice-authenticity --vault <path> [--draft <path>] [--json]` command.
- [ ] Smoke tests proving the command returns the right *shape* on a fixture vault (mix counts sum to the eligible-corpus size; leak fraction in `[0,1]`; `--draft` path populates `deslop`).
- [ ] `--json` emits a stable, documented schema.

## Examples

```console
$ creek voice-authenticity --vault ~/Vault
Voice authenticity — corpus of 412 eligible fragments
  audience mix:  OPEN 38  PERSONAL 351  INTIMATE 0 (excluded)   weighting: OFF
  AI-corpus leak: 263/412 (63.8%) fragments are claude/chatgpt ingests
  de-slop:       (no --draft given)
```

```python
report = build_voice_authenticity_report(vault, draft_path=None)
assert report.audience_mix.total == report.ai_corpus_leak.eligible_total
assert 0.0 <= report.ai_corpus_leak.fraction <= 1.0
assert report.audience_mix.weighting_active is False  # until Issue 03
```

## Constraints

**Scope fence:** Do NOT change ingestion, voice weighting, pattern extraction, or the de-slop
guard in this issue. This issue only *measures* current behavior. The numbers it prints should
expose the bugs (a high AI-corpus-leak fraction, `weighting: OFF`), not fix them.

**Anti-bypass (verbatim, non-negotiable):**

> No `noqa`, `# type: ignore`, `pylint: disable`, `eslint-disable`, or equivalent
> linter/type-checker silencers. Fix the root cause. The only exception is the documented 4-line
> escape hatch (third-party library bug / language-version compatibility / benchmarked
> performance necessity / generated code) — and it must include the reason, a reference URL, an
> alternative considered, and a review date. See the `max-quality-no-shortcuts` skill.

**Tracer-code invariant:** The command runs and prints all three sub-scores end-to-end on a real
vault after this issue; later issues only deepen the probes, never re-wire the surface.

## Definition of Done (stay-green)

- [ ] All new and existing tests pass (`cd creek-tools && ./scripts/test.sh --all`).
- [ ] `pre-commit run --all-files -c creek-tools/.pre-commit-config.yaml` is clean.
- [ ] Coverage on changed lines ≥90% branch; docstrings ≥95%; complexity ≤10; MyPy strict.
- [ ] Smoke test proves report shape on a fixture vault that contains both native and AI-chat fragments.
- [ ] PR body includes `Refs #EPIC_01_NUMBER` and `Closes #THIS_ISSUE_NUMBER`.
- [ ] Latest Claude reviewer `Verdict:` on HEAD is `LGTM`.

## Labels

`spec-decomposition`, `voice`, `cli`
