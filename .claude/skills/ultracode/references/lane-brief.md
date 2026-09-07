# Lane brief template

Hand this to every build lane. Fill the bracketed slots. The italicised
paragraphs are not padding — each one exists because omitting it cost a real
lane.

---

**Issue:** #[N] — [title]
**Worktree:** `[absolute path]` — work only here. Never `cd` to the repo root,
never `git checkout main`. Your branch and worktree already exist.
**Dossier:** `.ultracode/dossiers/issue-[N].md` — read it first.

### How to treat the dossier

> The **premise, evidence, acceptance criteria and refutations are
> authoritative.** The implementation plan is a **proposal to re-derive** against
> current source: every `file:line` may have shifted, so re-read before editing.
> **A premise of this brief being false is a finding to report, not a problem to
> work around.** If a sibling lane has merged since the dossier's HEAD
> ([sha]), these structural changes landed: [name them explicitly — a lane
> cannot diff against a HEAD it never saw].

### The bar

Every acceptance criterion in the dossier's checklist, not a subset. Gates 1–2.5
(TDD → local quality → pre-push self-review), then open the PR with
`Closes #[N]`.

- **Prove the test fails without the fix.** Stash the implementation, watch it go
  red with the expected signature, restore, confirm green. Put that in the PR.
- **Sweep the pattern, not the sample.** The reported location is one instance.
  Grep repo-wide and report **a table of every candidate site, including the safe
  ones and why they are safe.** "I found them all" is not evidence.
- **Assert on structure a transform cannot erase.** A content assertion
  downstream of a summariser, truncator or hash is vacuous.
- **Anti-bypass:** no `# noqa`, `# type: ignore`, `# pylint: disable`,
  `@pytest.mark.skip`, `--no-verify`; no lowered thresholds; no deleted tests.
  Fix the root cause.

### Known-red baseline

> These [N] tests fail locally on **any** branch in this repo and are CI-green:
> [named list]. **Any other failure is yours.**

*(Without that last sentence a lane will adopt an unrelated red as its own and
burn a cycle. With it, it will not.)*

### Environment

- Pre-provision the worktree venv fully before the first gate run — a sync alone
  is often not enough; export the venv path so the gate does not fall through to
  a system interpreter and misreport a real symbol as missing.
- Pin test workers: `[PYTEST_WORKERS=2 / repo equivalent]`. Do **not** use
  `-n auto` — you are one of several lanes and it would claim the whole machine.
- Delete stale coverage fragments before the gate; a killed prior run leaves
  files that make the next run die with an unrelated database error.
- Run the fast typechecker while iterating; run the full cold gate **once** at
  the end with a generous timeout. A gate cut off by a timeout is not a failure —
  check which lane it reached.
- **Pre-gate whole-tree linters before the long gate.** Dead-code, exception and
  refactor linters scan the *whole tree*, so one new function fails them eight
  minutes into a run that had otherwise passed.

### Reporting back

Return a structured verdict with:

- `verdict`: `PR_OPENED` | `BLOCKED` | `NO_CHANGE_NEEDED`
- `pr_url`, `branch`, `local_tip_sha`
- `premises_falsified`: what in the brief turned out to be wrong
- `sweep_table`: every candidate site and its disposition
- `notes_for_operator`: **anything that degraded.** In particular, say so
  plainly if you could not spawn a review agent and did your own Gate 2.5.

> **If `git push` is denied, report `BLOCKED` with the branch name and local tip
> SHA — do not stop quietly.** A lane that stops quietly looks identical to a
> lane that failed its gates. The orchestrator can complete the push. If you have
> already commented on a PR describing a commit the remote does not have, say so
> in a leading blockquote on that comment.

*(A lane that ends its turn "waiting on" a check has stopped prematurely. It is
recoverable with a resume message — but say what you are waiting on, so it can
be.)*
