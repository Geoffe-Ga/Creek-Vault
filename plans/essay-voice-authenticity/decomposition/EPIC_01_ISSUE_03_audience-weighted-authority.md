## Role

You are a senior Python engineer working in `creek-tools/creek/generate/voice.py`, fluent in the
FEAT-040 voice exemplar collection, ranking, and pattern aggregation pipeline.

## Goal

Give public-facing fragments **more authority** over the voice proxy. Replace the binary
privacy-tier gate with a **graduated audience-authority multiplier** that ranks `OPEN` (published,
public-facing) fragments above `PERSONAL` ones, keeps `INTIMATE` excluded by default, and finally
**consumes** the currently-ignored `representativeness` field. The multiplier must flow through
exemplar selection, ranking, and pattern aggregation so that the patterns of public work dominate
how drafts sound. Flip the diagnostic's `audience_weighting_active` to `True`.

## Context

- **Parent epic:** #EPIC_01_NUMBER
- **Predecessor issue(s):** #EPIC_01_ISSUE_01_NUMBER (diagnostic exposes `audience_mix`).
- **Findings:** SPEC summary §"Audience weight is unused".
- **Files involved:**
  - `creek-tools/creek/generate/voice.py` — `_eligible_register` (binary gate today),
    `VoiceExemplarCollector.rank_exemplars` / `_score`, and `VoiceProfileGenerator._rank_exemplars`;
    plus wherever per-fragment contributions are aggregated into patterns.
  - `creek-tools/creek/models.py` — `privacy_tier` (`OPEN/PERSONAL/INTIMATE`) and
    `representativeness` (`self/endorsed/aspirational/reference`).
  - `creek-tools/creek/config.py` — add the audience-weight knobs (see Issue 06 for full config
    surface; here add the minimal defaults this code reads).
  - `creek-tools/tests/test_voice_*` — weighting tests.
- **Prior decisions:** `INTIMATE` stays excluded by default (privacy fail-closed). The multiplier
  is *authority over patterns*, not a hard gate — a `PERSONAL` fragment still contributes, just
  less than an `OPEN` one. `representativeness=reference` content (borrowed) keeps its existing
  near-zero influence; `self`/`endorsed` rank above `aspirational`.

## Output Format

A single PR containing:

- [ ] An `audience_authority(fragment) -> float` helper combining `privacy_tier` and
  `representativeness` into a multiplier, with documented default weights.
- [ ] Exemplar selection/ranking and pattern aggregation multiply each fragment's contribution by
  `audience_authority`, so `OPEN` fragments win ties and dominate aggregated patterns.
- [ ] The diagnostic's `audience_mix.weighting_active` returns `True`.
- [ ] Tests: given an `OPEN` essay and a `PERSONAL` chat fragment expressing competing patterns,
  the generated voice profile reflects the `OPEN` fragment's pattern more strongly; `INTIMATE`
  remains excluded; `representativeness` changes the ranking as documented.

## Examples

```python
assert audience_authority(open_essay) > audience_authority(personal_chat)
profile = VoiceProfileGenerator(...).generate(vault)
# The OPEN fragment's distinctive habit outranks the PERSONAL one in the profile.
assert profile.dominant_for("sentence_length") == open_essay_signature
```

## Constraints

**Scope fence:** Do not add the citation-density pattern here — that is Issue 04 (which *uses* this
issue's multiplier). Do not change ingestion or the de-slop guard. Keep `INTIMATE` exclusion intact.

**Anti-bypass (verbatim, non-negotiable):**

> No `noqa`, `# type: ignore`, `pylint: disable`, `eslint-disable`, or equivalent
> linter/type-checker silencers. Fix the root cause. The only exception is the documented 4-line
> escape hatch (third-party library bug / language-version compatibility / benchmarked
> performance necessity / generated code) — and it must include the reason, a reference URL, an
> alternative considered, and a review date. See the `max-quality-no-shortcuts` skill.

**Backward-compat invariant:** a vault whose fragments are all the same tier and default
`representativeness` produces the same relative ranking as before (the multiplier is uniform → no
behavior change); only mixed-audience vaults shift.

## Definition of Done (stay-green)

- [ ] All new and existing tests pass (`cd creek-tools && ./scripts/test.sh --all`).
- [ ] `pre-commit run --all-files -c creek-tools/.pre-commit-config.yaml` is clean.
- [ ] Coverage on changed lines ≥90% branch; docstrings ≥95%; complexity ≤10; MyPy strict.
- [ ] Test proves an `OPEN` fragment outweighs a competing `PERSONAL` fragment in the generated profile.
- [ ] PR body includes `Refs #EPIC_01_NUMBER` and `Closes #THIS_ISSUE_NUMBER`.
- [ ] Latest Claude reviewer `Verdict:` on HEAD is `LGTM`.

## Labels

`spec-decomposition`, `voice`
