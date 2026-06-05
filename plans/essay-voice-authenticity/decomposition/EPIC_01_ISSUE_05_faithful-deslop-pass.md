## Role

You are a senior Python engineer working in `creek-tools/creek/generate/`, fluent in the FEAT-040.9
voice-fidelity guard (`ai_style/guard.py`), `DraftGenerator._apply_voice_fidelity`
(`generate/drafts.py`), and the `creek draft` CLI path (`cli.py`).

## Goal

Make the final AI-mannerism de-slop pass **execute faithfully and loudly** on the real draft path.
Today it can silently no-op (thin/unresolved fingerprint or disabled config → `return body, {}`),
and it only rewrites above the permissive `voice_distance_upper=0.35` ceiling — so a mannered draft
is measured and stamped but never stripped. Eliminate the silent no-ops, introduce a distinct
de-slop **target** that drives the rewrite loop, and prove with an integration test that an essay
seeded with known tells comes out of the real `creek draft` path with those tells **removed**.

## Context

- **Parent epic:** #EPIC_01_NUMBER
- **Predecessor issue(s):** #EPIC_01_ISSUE_01_NUMBER (diagnostic's `deslop` sub-score consumes this).
- **Findings:** SPEC summary §"The de-slop pass runs but often no-ops silently".
- **Files involved:**
  - `creek-tools/creek/generate/drafts.py` — `_apply_voice_fidelity` (the silent
    `if config is None or fingerprint is None or not config.enabled: return body, {}`), and its
    callers `save_draft` / `save_outline_draft`.
  - `creek-tools/creek/generate/ai_style/guard.py` — `run_voice_fidelity_guard` (rewrite loop
    gated on `voice_distance_upper`; `thin_fingerprint` flag).
  - `creek-tools/creek/config.py` — `AIStyleConfig` (`enabled=True`, `voice_distance_upper=0.35`);
    add `voice_distance_target`.
  - `creek-tools/creek/cli.py` — `_resolve_voice_fingerprint` (returns `None`/thin silently);
    draft command at `cli.py:2634`.
  - `creek-tools/tests/test_drafts.py`, `tests/generate/ai_style/`.
- **Prior decisions:** `voice_distance_upper` remains the accepted-divergence **ceiling**; the new
  `voice_distance_target` is the value the rewrite loop drives toward. The guard must always leave
  a machine-readable status, even on a no-op.

## Output Format

A single PR containing:

- [ ] Every de-slop no-op is **loud**: when the fingerprint is unresolved/thin, config disabled, or
  no rewrite occurs, emit a clear stderr diagnostic AND stamp a `voice_guard_status` reason on the
  draft frontmatter (e.g., `skipped:no_fingerprint`, `measured_only:below_ceiling`, `rewritten`).
  No path returns silently-unchanged prose without a recorded reason.
- [ ] A `voice_distance_target` config field; the rewrite loop drives toward the target (when an LLM
  is available and `--no-llm` is not set) rather than only firing above the ceiling.
- [ ] An **integration test on the real path**: draft an essay whose seed/source contains planted
  tells ("rich tapestry", "delve", "it's not X, it's Y"); after `save_draft` through
  `DraftGenerator` with a resolved fingerprint, assert the planted tells are **absent** from the
  written file (not merely that `voice_distance` was stamped).
- [ ] The diagnostic's `deslop.status` reflects the real guard outcome for a given `--draft`.

## Examples

```python
gen = DraftGenerator(llm=stub_rewriter, fingerprint=real_fp, ai_style_config=AIStyleConfig())
path = gen.save_draft(draft_with_tells, vault_path)
text = path.read_text()
assert "rich tapestry" not in text and "delve" not in text   # stripped, not just measured
post = frontmatter.loads(text)
assert post["voice_guard_status"] in {"rewritten", "measured_only:below_target"}
```

```python
# No silent skip: a thin fingerprint is reported, never swallowed.
body, fields = gen_thin._apply_voice_fidelity(body, vault_path=vp, source_fragments=())
assert fields["voice_guard_status"].startswith("skipped:")
```

## Constraints

**Scope fence:** Do not rewrite the distance metric / scanner internals or the tell catalog — fix
faithful *execution and observability*, not the algorithm. Do not touch ingestion or audience
weighting.

**Anti-bypass (verbatim, non-negotiable):**

> No `noqa`, `# type: ignore`, `pylint: disable`, `eslint-disable`, or equivalent
> linter/type-checker silencers. Fix the root cause. The only exception is the documented 4-line
> escape hatch (third-party library bug / language-version compatibility / benchmarked
> performance necessity / generated code) — and it must include the reason, a reference URL, an
> alternative considered, and a review date. See the `max-quality-no-shortcuts` skill.

**Determinism invariant:** with `--no-llm`, the guard still sanitizes + scans + stamps a status and
performs no LLM rewrite; behavior stays deterministic and offline.

## Definition of Done (stay-green)

- [ ] All new and existing tests pass (`cd creek-tools && ./scripts/test.sh --all`).
- [ ] `pre-commit run --all-files -c creek-tools/.pre-commit-config.yaml` is clean.
- [ ] Coverage on changed lines ≥90% branch; docstrings ≥95%; complexity ≤10; MyPy strict.
- [ ] Integration test proves planted tells are removed on the real `save_draft` path; a separate test proves a thin fingerprint is reported, never silently swallowed.
- [ ] PR body includes `Refs #EPIC_01_NUMBER` and `Closes #THIS_ISSUE_NUMBER`.
- [ ] Latest Claude reviewer `Verdict:` on HEAD is `LGTM`.

## Labels

`spec-decomposition`, `voice`
