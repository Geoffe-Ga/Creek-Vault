## Role

You are a senior Python engineer working in `creek-tools/creek/generate/voice.py`, fluent in the
FEAT-040 voice-skill generation pipeline.

## Goal

Make `creek skills generate` exclude all `11-Other-Authors/` content from the voice corpus by
gating eligibility on `voice_weight > 0`, with `ai-as-user` excluded by default — so the voice
proxy is never trained on borrowed or AI-authored text (preventing voice drift / model collapse).

## Context

- **Parent epic:** #EPIC_01_NUMBER
- **Predecessor issue(s):** #EPIC_01_ISSUE_03_NUMBER (the `voice_weight` field must exist).
- **SPEC section:** §7.5 (voice-training exclusion), §13 open question #2.
- **Files involved:**
  - `creek-tools/creek/generate/voice.py` — corpus-selection gate.
  - `creek-tools/tests/` — exclusion tests.
- **Prior decisions:** Default `ai-as-user` `voice_weight=0.0` (excluded). Raising it is an explicit, audited opt-in — out of scope here; just make the gate honor whatever `voice_weight` is set. This is the voice-fidelity analogue of the privacy fail-closed rule.
- **State of the world:** Voice generation currently selects from `01-Fragments/` / `07-Voice/`. It does not yet know about `voice_weight` or `11-Other-Authors/`.

## Output Format

A single PR containing:

- [ ] Corpus selection gates on `voice_weight > 0` and excludes `11-Other-Authors/` by path.
- [ ] Test: a fixture vault with native fragments + an `11-Other-Authors/` author + an `ai-as-user` piece → generated voice corpus contains only the native fragments.
- [ ] A guard test asserting `ai-as-user` content (`voice_weight=0.0`) is excluded.

## Examples

```python
corpus = collect_voice_corpus(vault)
assert all("11-Other-Authors" not in str(frag.path) for frag in corpus)
assert all(frag.voice_weight > 0 for frag in corpus)
```

## Constraints

**Scope fence:** Do not implement the audited opt-in mechanism for raising `ai-as-user` weight
(future work). Only enforce the gate on the existing field value.

**Anti-bypass (verbatim, non-negotiable):**

> No `noqa`, `# type: ignore`, `pylint: disable`, `eslint-disable`, or equivalent
> linter/type-checker silencers. Fix the root cause. The only exception is the documented 4-line
> escape hatch (third-party library bug / language-version compatibility / benchmarked
> performance necessity / generated code) — and it must include the reason, a reference URL, an
> alternative considered, and a review date. See the `max-quality-no-shortcuts` skill.

**Tracer-code invariant:** Voice generation on a vault without `11-Other-Authors/` is unchanged.

## Definition of Done (stay-green)

- [ ] All new and existing tests pass (`cd creek-tools && ./scripts/test.sh --all`).
- [ ] `pre-commit run --all-files -c creek-tools/.pre-commit-config.yaml` is clean.
- [ ] Coverage on changed lines ≥90% branch; docstrings ≥95%; complexity ≤10; MyPy strict.
- [ ] Exclusion proven by test on a fixture containing other-author + ai-as-user content.
- [ ] PR body includes `Refs #EPIC_01_NUMBER` and `Closes #THIS_ISSUE_NUMBER`.
- [ ] Latest Claude reviewer `Verdict:` on HEAD is `LGTM`.

## Labels

`spec-decomposition`, `core`, `voice`
