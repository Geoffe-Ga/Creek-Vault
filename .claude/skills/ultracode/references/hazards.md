# Hazard catalog

Failures that look like success, and misreadings that cost hours. Consult when a
lane reports something strange, or when a gate looks suspiciously green.

---

## Green things that are not green

- **"no files to check" / "0 tests collected" / "skipped" / an instant pass /
  a $0 cost.** All look like success; none are. Re-run explicitly against the
  files you changed and read the collected count.
- **One bad path makes pytest run nothing.** A single mistyped test file collects
  zero tests and reads as a fast pass. Derive test paths from `git`, then read
  the count.
- **A stale lint cache gives a false-clean gate.** Verify with the tool's
  no-cache flag; CI has no cache and will see the truth.
- **A test that passes against unfixed code proves nothing.** Break the code and
  watch it go red, every time.
- **Emptying a `parametrize` list silently skips.** Closing the last case makes a
  whole security suite vanish behind a green gate. Assert zero SKIPPED.
- **A self-healing reader makes its own test vacuous.** If the reader repairs a
  bad index on lookup, assert the raw bytes on disk.
- **A guard that names its tool satisfies its own test.** A probe line mentioning
  the auditor counted as the audit. Mutate by deleting the *real* invocation.
- **A "rejects noise" test is not a "rejects wrong input" test.** Test the
  off-diagonal: every decoder against every other encoder's output.
- **Stale byte-compiled caches are still read** even with "don't write bytecode"
  set. Use a cache-prefix env var to isolate.
- **Stale coverage fragments** from a killed run make the next gate die with an
  unrelated database error. Delete them.

## Misread signals

- **A resource number pinned rather than fluctuating is stale, not binding.**
  Check whether anything is actually running before believing it.
- **`memory_pressure` reads ~65% free while the box is dying** (counts
  reclaimable pages). **Swap-free reads "starving" while the box is idle**
  (stale high-water mark that never moves after the fleet exits). Decide on
  *available* RAM; print swap only as a divergence tell.
- **A CI failure with `null` steps** after setup, `BlobNotFound` logs and
  wall-clock far off is **runner eviction**. Re-run; do not debug.
- **A timeout on an unreadable CI rollup proves nothing about the tree** — only
  that the classifier could not tell. Never convert it into a fix dispatch.
- **`mergeStateStatus: CLEAN` says nothing about staleness.** Use `behind_by`.
- **A false `behind` from a stale local ref destroys an LGTM for nothing.**
  `git fetch` and re-check against the API first. And a *real* `behind` is still
  mergeable when the file sets are disjoint.
- **Lane verdict fields describe the run's start**, not its end. Read
  `findings[].fixed`.
- **A background task's exit code may be the wrapper's, not the work's.** Write
  the real exit code into the log and read that.
- **Local-only greens.** Tests deriving from `cpu_count` or wall-clock pass on a
  10-core laptop and fail on a 4-core runner. Model defaults using a local
  timezone fail on UTC CI for part of every day — verify with a TZ matrix.

## Shell and process traps

- **The Bash tool's cwd persists across calls.** A `cd` into a worktree turns a
  later relative path into exit 127 — and arming *looks* like it worked. Use
  absolute paths everywhere; `cd` back in the same command.
- **A failed `cd` silently reads another lane as your own.** Distrust
  surprisingly extreme readings.
- **`grep -c` exits 1 on zero matches**, so `x=$(cmd | grep -c foo || echo 0)`
  yields `"0\n0"` and any `!= "0"` test fires a false alarm. Use
  `if cmd | grep -q foo;`.
- **zsh silently changes the command you wrote.** An unmatched glob aborts the
  whole chain; an unquoted multi-path variable becomes one argument.
- **A slice anchor can match an earlier occurrence**, duplicating a block and
  shadowing a test. 144 passing tests proved nothing.
- **Mutation scripts must snapshot, not `git checkout`** — a mid-battery restore
  deletes the lane's own uncommitted implementation.
- **An under-provisioned venv looks like a script bug.** A missing tool makes a
  bare tool name fall through PATH to a system copy. Check the venv's `bin/`
  first. Smoke-test a gate from the cwd it will actually run in.

## Process traps

- **Lanes collide in a shared scratchpad.** Issue-scope every filename, or one
  PR gets another issue's body.
- **Subagent `git push` denial is intermittent, not deterministic.** Check
  `gh pr list --head <branch>` before opening a PR or you get a duplicate.
- **Prose closes issues.** GitHub honours any `fixes #N` in a PR body regardless
  of grammar. Write deferred mentions as `re: #N`.
- **Specialists delete live assertions** and call them their own artifacts. Diff
  against `git show HEAD:<path>` before accepting a test edit.
- **Machine-wide lessons are project-scoped memories.** A lesson about the *box*
  must be written into every project that runs a fleet, or the next project
  relearns it by breaking the machine.
