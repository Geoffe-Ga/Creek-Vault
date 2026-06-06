## Role

You are a senior Python engineer working across `creek-tools/` config, ingestion-refresh, docs, and
end-to-end tests. You harden a feature set into something an existing-vault owner can adopt safely.

## Goal

Close the epic: expose the new behavior through documented config, give existing vaults a migration
path that re-splits previously-merged AI-chat fragments, update the docs, and land one end-to-end
regression test that ties all three fixes together and drives the `creek voice-authenticity`
diagnostic to a clean bill of health.

## Context

- **Parent epic:** #551
- **Predecessor issue(s):** #553, #554,
  #555, #556 (all of the behavior this issue documents,
  configures, and proves end-to-end).
- **Files involved:**
  - `creek-tools/creek/config.py` — consolidate the audience-weight knobs (Issue 03), citation
    weighting (Issue 04), and `voice_distance_target` (Issue 05) into a coherent, documented config
    surface with sane defaults; validate ranges.
  - `creek-tools/creek/ingest/refresh.py` — re-ingest path that re-splits existing single-fragment
    AI-chat content into per-turn attributed fragments (idempotent; safe to re-run).
  - `creek-tools/docs/ingestion.md`, `docs/generation.md`, `docs/classification.md`,
    `docs/configuration.md` — document attribution, audience weighting, citation density, the
    de-slop status field, and the `voice-authenticity` command.
  - `creek-tools/tests/e2e/` — the end-to-end regression.
- **Prior decisions:** migration is opt-in via `creek ingest --refresh` (or equivalent), never an
  implicit mutation. The end-to-end test is the epic's acceptance proof.

## Output Format

A single PR containing:

- [ ] A documented config surface for audience weights, citation weighting, and
  `voice_distance_target` with validated defaults; a config round-trip test.
- [ ] A re-ingest/migration path that converts existing merged AI-chat fragments into per-turn
  attributed fragments, idempotently; tested on a fixture vault that starts with merged
  `source.author=self` chat fragments and ends with quarantined AI turns.
- [ ] Docs updated across ingestion / generation / classification / configuration.
- [ ] One end-to-end regression: ingest an AI chat **and** an `OPEN` citation-heavy essay → generate
  voice → draft an essay with planted tells → assert (a) AI turns excluded from the corpus,
  (b) `OPEN` citation habit reflected in the voice skill, (c) planted tells stripped from the draft,
  (d) `creek voice-authenticity` reports low AI-leak, `weighting: ON`, and a `rewritten`/clean
  de-slop status.

## Examples

```console
$ creek ingest --refresh --vault ~/Vault     # re-splits old merged chats; idempotent
$ creek voice-authenticity --vault ~/Vault
  audience mix:  OPEN 38  PERSONAL 351  INTIMATE 0 (excluded)   weighting: ON
  AI-corpus leak: 2/412 (0.5%)
  de-slop:       rewritten (3 tells removed)
```

## Constraints

**Scope fence:** No new voice behavior here — only config surface, migration, docs, and the
end-to-end proof. If a predecessor issue's behavior is missing, fix it *there*, not by widening this
issue.

**Anti-bypass (verbatim, non-negotiable):**

> No `noqa`, `# type: ignore`, `pylint: disable`, `eslint-disable`, or equivalent
> linter/type-checker silencers. Fix the root cause. The only exception is the documented 4-line
> escape hatch (third-party library bug / language-version compatibility / benchmarked
> performance necessity / generated code) — and it must include the reason, a reference URL, an
> alternative considered, and a review date. See the `max-quality-no-shortcuts` skill.

**Idempotence invariant:** running the migration twice yields the same vault state as running it
once; a vault with no merged chat fragments is unchanged.

## Definition of Done (stay-green)

- [ ] All new and existing tests pass (`cd creek-tools && ./scripts/test.sh --all`).
- [ ] `pre-commit run --all-files -c creek-tools/.pre-commit-config.yaml` is clean.
- [ ] Coverage on changed lines ≥90% branch; docstrings ≥95%; complexity ≤10; MyPy strict.
- [ ] The end-to-end regression asserts all four acceptance points above.
- [ ] `cd creek-tools && ./scripts/check-all.sh` is green.
- [ ] PR body includes `Refs #551` and `Closes #557`.
- [ ] Latest Claude reviewer `Verdict:` on HEAD is `LGTM`.

## Labels

`spec-decomposition`, `voice`, `ingest`
