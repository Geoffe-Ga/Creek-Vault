# Spec Decomposition Templates

Two templates: the epic body and the issue body. Both are designed to be passed to `gh issue create --body-file`.

The issue template is the load-bearing one — it is a **prompt**, not a description. Every section must be filled in. If a section is genuinely empty, the issue is not ready to file.

## Epic Body Template

Write to `git-issues/EPIC_NN_slug.md`:

```markdown
## Epic Summary

[2-3 sentences: what this workstream delivers and why it matters. Quote
the relevant SPEC section by heading or line range.]

## Scope

**In scope:**
- [Bullet 1]
- [Bullet 2]

**Out of scope:**
- [Bullet 1 — will be a separate epic or follow-up issue]

## Success Criteria

The epic is done when:

- [ ] [Observable system-level outcome 1]
- [ ] [Observable system-level outcome 2]
- [ ] All child issues are closed
- [ ] Smoke tests for the full epic surface pass on `main`

## Child Issues

_Filled in after child issues are filed (Step 8/9 of spec-decomposition)._

- [ ] #NNN — Skeleton: [brief description]
- [ ] #NNN — Core: [brief description]
- [ ] #NNN — Edges: [brief description]
- [ ] #NNN — Polish: [brief description]

## Sequencing Notes

[Any dependencies on other epics, parallel work that is safe, or work
that must block on this epic finishing.]

## SPEC Reference

[Link or path to the source SPEC, plus heading/line range for the
section this epic covers.]

## Labels

`epic`, `spec-decomposition`, plus one domain label (e.g. `auth`, `billing`).
```

## Issue Body Template (the Prompt)

Write to `git-issues/EPIC_NN_ISSUE_MM_slug.md`:

```markdown
## Role

You are a [specific role with the relevant stack expertise — e.g.,
"Senior FastAPI engineer working in this repo's `src/billing/` module"].

## Goal

[One sentence stating the specific, measurable outcome. Should be
verifiable by a test or an observable system behavior.]

## Context

- **Parent epic:** #EPIC_NN_NUMBER
- **Predecessor issue(s):** #PRED_NUMBER (must be merged first), or "none — this is the skeleton issue"
- **SPEC section:** [path/to/spec.md heading or line range]
- **Files involved:**
  - `path/to/file_a.py` — [what role it plays]
  - `path/to/file_b.py` — [what role it plays]
- **Prior decisions:** [any architectural-decisions output, ADR links, or
  decisions captured in earlier issues that constrain this work]
- **State of the world:** [what currently exists in those files — stubs?
  partial implementation? nothing yet?]

## Output Format

Deliverable is a single PR containing:

- [ ] Production code changes in [files]
- [ ] New or updated tests in [test files] proving the goal
- [ ] Docstring / doc updates where public API changes
- [ ] No drive-by changes unrelated to the goal

## Examples

[Concrete example of what success looks like. Pick one:]

**Example: request/response**
```
POST /billing/calculate
{ "impressions": 1000, "cpm": 5.0 }

200 OK
{ "cost": 5.0, "currency": "USD" }
```

**Example: test case that should pass after this issue lands**
```python
def test_calculate_cost_from_impressions():
    result = calculate_cost(impressions=1000, cpm=5.0)
    assert result == 5.0
```

## Constraints

**Scope fence:** Do not implement [explicitly out-of-scope thing — that
belongs to issue #X]. If you find yourself touching files outside the
list above, stop and check with the user.

**Anti-bypass (verbatim, non-negotiable):**

> No `noqa`, `# type: ignore`, `pylint: disable`, `eslint-disable`, or
> equivalent linter/type-checker silencers. Fix the root cause. The only
> exception is the documented 4-line escape hatch (third-party library
> bug / language-version compatibility / benchmarked performance
> necessity / generated code) — and it must include the reason, a
> reference URL, an alternative considered, and a review date. See the
> `max-quality-no-shortcuts` skill.

**Tracer-code invariant:** The system must remain demoable after this PR
merges. If your change breaks an unrelated endpoint or CLI surface, you
have gone outside scope — revert and re-plan.

## Definition of Done (stay-green)

- [ ] All new and existing tests pass (`./scripts/test.sh --all` or repo
  equivalent).
- [ ] `pre-commit run --all-files` is clean — no skipped hooks, no
  bypassed checks.
- [ ] Coverage on changed lines meets the repo threshold (default 90%).
- [ ] Public API changes are reflected in docstrings and any
  user-facing docs.
- [ ] PR body uses git-workflow's PR template and includes:
  - `Refs #EPIC_NN_NUMBER` (link to the epic)
  - `Closes #THIS_ISSUE_NUMBER` (auto-close on merge)
- [ ] If the repo runs the Claude reviewer GitHub Action: latest
  `Verdict:` on HEAD is `LGTM`.

## Labels

`spec-decomposition`, plus phase label (`tracer-skeleton` | `core` |
`edges` | `polish`), plus domain label.
```

## Filing Order Reminder

```
1. Epics first.        gh issue create --body-file EPIC_NN_*.md
2. Capture numbers.    EPIC_NN_NUMBER=<returned-number>
3. sed substitute      sed -i "s/EPIC_NN_NUMBER/$EPIC_NN_NUMBER/g" \
                         git-issues/EPIC_NN_ISSUE_*.md
4. Children next.      gh issue create --body-file EPIC_NN_ISSUE_*.md
5. Edit epic body.     gh issue edit <epic-number> --body-file <updated-file>
                       (fills in the Child Issues checklist with real numbers)
```

Never inline issue bodies into `--body` strings. The file is the
artifact — it's how the user reviews the decomposition before filing and
how the decomposition stays auditable after the fact.
