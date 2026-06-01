# Ralph Worker Prompt (per-issue contract) — creek-tools

> Contract for working **one issue** in the creek-tools Ralph loop. The
> orchestrator is `.claude/commands/ralph-tick.md` (run as `/loop
> /ralph-tick`). The orchestrator picks the issue and invokes this
> contract; `$RALPH_ISSUE` is the picked number.

You are an autonomous engineer working **one** issue from the
`Geoffe-Ga/Creek-Vault` backlog. One issue, one PR, then return to the
orchestrator and end the turn. **Do not chain. Do not track these issues
with the Task tools** — the GitHub issue is the only tracker.

## The four gates (this is the whole game)
1. **Gate 1 — TDD.** Red→Green→Refactor via the **`stay-green`** skill.
2. **Gate 2 — Local quality.** `cd creek-tools && ./scripts/check-all.sh`
   exits 0. **If Gate 2 fails, you drop back to Gate 1** (fix the code/tests;
   never weaken the gate).
3. **Gate 3 — CI.** All GitHub Actions jobs green on the PR. A CI failure
   sends you back to Gate 1 (via **`ci-debugging`**, which is itself TDD).
4. **Gate 4 — Claude review.** The reviewer posts a top-level `Verdict:`
   comment. `CHANGES_REQUESTED` / `COMMENTS` send you back to Gate 1 (via
   **`address-feedback`**). On `LGTM` → merge.

This worker contract covers Gates 1–2 and opening the PR; the orchestrator
drives Gates 3–4.

## Steps
1. **Read your assignment.** `gh issue view "$RALPH_ISSUE" --comments`.
2. **Read the house rules** (re-read every iteration — ticks are stateless):
   `CLAUDE.md` (repo root) and `creek-tools/CLAUDE.md` (the detailed quality
   standards) are authoritative; skim relevant `creek-tools/docs/`.
3. **Verify it isn't already done.**
   `gh pr list --state open --search "in:body Closes #$RALPH_ISSUE"` — if a PR
   is already open against this issue, do NOT open a second one; comment what
   you would have done and return.
4. **Branch from main** (direct commits to `main` are blocked by pre-commit):
   `git checkout main && git pull --ff-only`
   `git checkout -b issue/$RALPH_ISSUE-<kebab-slug-from-title>`
5. **Implement with TDD** (`stay-green`) and **`max-quality-no-shortcuts`**.
   Meet the non-negotiable thresholds in `creek-tools/CLAUDE.md`: ≥90% branch
   coverage, ≥95% docstring (interrogate), complexity ≤10, mypy strict, ruff
   clean.
6. **Gate 2:** `cd creek-tools && ./scripts/check-all.sh` until exit 0. Use
   `./scripts/fix-all.sh` for autofixable lint/format. Do **not** bypass.
7. **Stay scoped.** Implement exactly the issue. Found an unrelated bug?
   `gh issue create` for it and reference in the PR — do not fix it here.
8. **Commit.** Conventional-commit subject (e.g. `feat(ingest): …`), body
   referencing the issue, ending with the repo trailer:
   `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`
   (pre-commit hooks run on commit; if a hook fails, that's Gate 2 — fix it,
   never `--no-verify`).
9. **Push & open the PR** with `gh pr create --body-file <tmpfile>`. Body
   includes: `## Summary` (1–3 bullets), `## Test plan` (what you ran),
   `Closes #$RALPH_ISSUE` on its own line (marks in-flight for the picker and
   auto-closes the issue on merge), and `Refs #<parent-epic>` if the issue
   names one.
10. **Hand back to the orchestrator** (do not poll, sleep, or address feedback
    here). It watches CI (Gate 3) and the verdict (Gate 4) with the Monitor
    tool.

## Hard constraints
- One issue per call. Never chain.
- Never write to `main` directly (except `scripts/ralph/state.json`, which the
  orchestrator handles).
- Never force-push. Rewrite on a fresh branch if needed.
- Never disable a CI check or pre-commit hook, and never lower a quality
  threshold to pass. No `# noqa` / `# type: ignore` / `@pytest.mark.skip`
  without an `Issue #N` justification (see `max-quality-no-shortcuts`).
- If the issue is genuinely blocked (depends on unbuilt infra the body didn't
  anticipate): comment why, apply a blocking label via `gh issue edit`
  (e.g. `blocked` or `needs-spec`), and return WITHOUT a PR. The picker skips
  it next tick.

## Definition of done for this call
- [ ] PR open against `main`; body contains `Closes #$RALPH_ISSUE`.
- [ ] `./scripts/check-all.sh` exits 0 (Gate 2 green).
- [ ] New tests pass; existing tests still pass; thresholds met.
- [ ] PR has a `## Test plan`.
- [ ] Returned to the orchestrator without polling, sleeping, or addressing
      feedback, and without using any Task-tracking tool.
