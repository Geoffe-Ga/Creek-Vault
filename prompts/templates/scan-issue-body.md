<!--
  Canonical body template for every autonomous-maintenance scan issue.

  Each scan issue is itself a prompt: it is consumed by the Ralph agent, so it
  follows the 6-component framework (Role / Goal / Context / Output Format /
  Examples / Constraints) exactly. The scan-issue-writer skill fills every
  component verbatim — an issue missing any component is labeled `needs-triage`
  instead of `agent-ready`, and the grooming pass finishes it.

  Replace every [bracketed] placeholder. Leave no placeholder behind.
-->

## Role
You are a senior Python engineer working in this project's codebase, following
its existing conventions (TDD via stay-green, the `cd creek-tools &&
./scripts/check-all.sh` gate, ≥90% branch coverage aggregate and ≥80% per file,
≥95% docstring coverage, complexity ≤10, mypy strict, zero lint/type
suppressions).

## Goal
[One sentence. Specific, measurable, verifiable. e.g. "Eliminate the O(n²)
pairwise loop in link.resonance by vectorising similarity scoring, verified by
an equivalence test against the current output on a fixture vault."]

## Context
- File(s): `path/to/file.py:120-164`
- Scanned at commit: `<SHA>` — re-verify against HEAD before starting
- Evidence: [tool output excerpt — the radon score, the audit finding, the
  coverage gap, the profile, the grep hit with surrounding lines]
- Related: [links to sibling issues from the same scan run, prior PRs]

## Output Format
A single PR that: (1) adds a failing test first, (2) makes it pass, (3) passes
`cd creek-tools && ./scripts/check-all.sh`, and (4) references this issue with
"Closes #N".

## Examples
[One concrete before/after sketch — e.g. the current pairwise loop vs. the
vectorised version. 5–15 lines. Enough to disambiguate, not a full spec.]

## Constraints
- Do not change public API signatures unless the Goal says so
- No lint/type suppressions (max-quality-no-shortcuts): fix root causes
- Scope: this issue only — file follow-up issues for adjacent problems
- If the finding no longer reproduces at HEAD, close this issue with a comment
  explaining what changed instead of forcing a PR
