## Role

You are a senior Python engineer optimizing an Anthropic SDK application, fluent in prompt caching,
model tiering, and this repo's docs conventions.

## Goal

Polish the desk for cost and operability: add prompt caching on the large static context (ontology
spec, medium contract, voice skills), make per-agent model tiers configurable, add a cost guard
(cache-hit assertions), and document the writing desk for users.

## Context

- **Parent epic:** #EPIC_02_NUMBER
- **Predecessor issue(s):** #EPIC_02_ISSUE_06_NUMBER (the desk is functionally complete).
- **SPEC section:** §4.4 (prompt caching + model tiering), §13 open questions #1 (cost budget) and #4 (bounds defaults documented).
- **Files involved:**
  - `creek-tools/creek/author/` — caching + tiering config.
  - creek config / `crawdad.yaml` schema — model IDs + `max_author_rounds`.
  - `creek-tools/docs/` — a `writing-desk.md` user guide; update `generation.md` if present.
  - `creek-tools/tests/` — cache-hit + config tests.
- **Prior decisions:** Haiku for routing/classification sub-tasks, Sonnet for synthesis/voicing, reflection configurable. Model IDs always from config, never hard-coded. Open question #1 default: rely on tiering + caching, no hard per-medium budget yet (note it as future work).
- **State of the world:** The desk works but may re-send static context each call and hard-codes tiers.

## Output Format

A single PR containing:

- [ ] Prompt caching on the static context; cache-hit assertions in a test.
- [ ] Configurable per-agent model tiers (config-driven).
- [ ] `docs/writing-desk.md` user guide (CLI usage, mediums, attribution, escalation).
- [ ] Tests: cache hit on repeated static context; config overrides respected.

## Examples

```python
stats = run_author(medium="research", query="...", vault=fixture).usage
assert stats.cache_read_tokens > 0     # static context served from cache on 2nd+ call
```
```bash
creek author --medium research --query "..." --vault ~/Creek   # documented in docs/writing-desk.md
```

## Constraints

**Scope fence:** No new features or mediums — tighten what exists. Do not change agent behavior,
only cost/operability + docs.

**Anti-bypass (verbatim, non-negotiable):**

> No `noqa`, `# type: ignore`, `pylint: disable`, `eslint-disable`, or equivalent
> linter/type-checker silencers. Fix the root cause. The only exception is the documented 4-line
> escape hatch (third-party library bug / language-version compatibility / benchmarked
> performance necessity / generated code) — and it must include the reason, a reference URL, an
> alternative considered, and a review date. See the `max-quality-no-shortcuts` skill.

**Tracer-code invariant:** Behavior is unchanged; only cost, configurability, and docs improve.

## Definition of Done (stay-green)

- [ ] All new and existing tests pass (`cd creek-tools && ./scripts/test.sh --all`).
- [ ] `pre-commit run --all-files -c creek-tools/.pre-commit-config.yaml` is clean.
- [ ] Coverage on changed lines ≥90% branch; docstrings ≥95%; complexity ≤10; MyPy strict.
- [ ] Cache-hit asserted by test; docs build/lint clean.
- [ ] PR body includes `Refs #EPIC_02_NUMBER` and `Closes #THIS_ISSUE_NUMBER`.
- [ ] Latest Claude reviewer `Verdict:` on HEAD is `LGTM`.

## Labels

`spec-decomposition`, `polish`, `author-desk`
