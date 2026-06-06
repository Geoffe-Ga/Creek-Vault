## Role

You are a senior Python engineer working in `creek-tools/creek/generate/voice.py`, fluent in
`VoicePatternExtractor` and how extracted patterns are rendered into the generated voice skill.

## Goal

Teach the voice proxy the owner's most-missed habit: **referencing and quoting source material**.
Add a citation/quotation/reference-density detector to `VoicePatternExtractor`, surface it in the
generated voice skill, and weight it by the audience-authority multiplier from Issue 03 — so the
heavy-citation habit of *public* work (where it is prevalent) shapes drafts, while private writing
(where it is rare) does not dilute it.

## Context

- **Parent epic:** #551
- **Predecessor issue(s):** #554 (provides `audience_authority`, which this
  pattern must be weighted by).
- **Findings:** SPEC summary §"Citation/quotation density is unmeasured".
- **Files involved:**
  - `creek-tools/creek/generate/voice.py` — `VoicePatternExtractor.extract_patterns` (add the
    detector alongside the existing punctuation/rhetorical-move metrics), and the
    pattern→voice-skill rendering path.
  - `creek-tools/tests/test_voice_patterns.py` and the generated-skill assertions.
- **What "citation density" means here (detect at least these, normalized per ~1000 words):**
  - quotation spans (paired quote marks / blockquote lines),
  - attribution phrases ("according to", "as X notes/writes/argues", "in X's words"),
  - reference markers (`[1]`, `(Smith, 2023)`, footnote refs),
  - outbound source links.
- **Prior decisions:** this is a *measured pattern* that informs the voice skill — it is NOT the
  de-slop guard (Issue 05) and NOT a hard filter. Aggregate it with the same audience multiplier
  as every other pattern so `OPEN` fragments dominate the citation signal.

## Output Format

A single PR containing:

- [ ] A `citation_density` metric on the extracted pattern struct, computed from the signals above and normalized per word count.
- [ ] The generated voice skill surfaces the owner's citation tendency (e.g., a "you reference and quote sources at roughly N× the baseline rate" line / exemplar selection that favors citation-bearing passages from `OPEN` work).
- [ ] The metric is aggregated using Issue 03's `audience_authority`, so public-work citation habits dominate.
- [ ] Tests: a corpus of citation-heavy `OPEN` fragments + citation-light `PERSONAL` fragments yields a high aggregated `citation_density`; an all-private corpus yields a low one; the rendered skill reflects the difference.

## Examples

```python
patterns = VoicePatternExtractor().extract_patterns(corpus)
assert patterns.citation_density > baseline           # owner cites heavily
# Public work dominates the signal even when private fragments outnumber it:
assert patterns.citation_density == approx(public_weighted_density, abs=...)
```

## Constraints

**Scope fence:** Detection + surfacing only. Do not make drafts *insert* citations and do not add a
de-slop rule about citations — generation/remediation is out of scope. Reuse Issue 03's multiplier;
do not re-derive audience weighting here.

**Anti-bypass (verbatim, non-negotiable):**

> No `noqa`, `# type: ignore`, `pylint: disable`, `eslint-disable`, or equivalent
> linter/type-checker silencers. Fix the root cause. The only exception is the documented 4-line
> escape hatch (third-party library bug / language-version compatibility / benchmarked
> performance necessity / generated code) — and it must include the reason, a reference URL, an
> alternative considered, and a review date. See the `max-quality-no-shortcuts` skill.

**Tracer-code invariant:** extracting patterns from a corpus with no citations yields
`citation_density == 0.0` and changes nothing else about the existing pattern output.

## Definition of Done (stay-green)

- [ ] All new and existing tests pass (`cd creek-tools && ./scripts/test.sh --all`).
- [ ] `pre-commit run --all-files -c creek-tools/.pre-commit-config.yaml` is clean.
- [ ] Coverage on changed lines ≥90% branch; docstrings ≥95%; complexity ≤10; MyPy strict.
- [ ] Test proves citation-heavy public work raises the aggregated metric while private-only work does not.
- [ ] PR body includes `Refs #551` and `Closes #555`.
- [ ] Latest Claude reviewer `Verdict:` on HEAD is `LGTM`.

## Labels

`spec-decomposition`, `voice`
