#!/usr/bin/env bash
# scripts/ralph/backlog-gate.sh
#
# The ONE measurement of Ralph's agent-ready backlog, and the ONE ceiling it is
# measured against. Prints exactly one verdict line (stdout, and
# $GITHUB_STEP_SUMMARY when set), writes `count`, `ceiling` and `proceed` to
# $GITHUB_OUTPUT, and exits 0 on either verdict — a full queue is a deliberate
# stand-down, not a failure.
#
# ---------------------------------------------------------------------------
# WHY THIS EXISTS (issue #1516)
# ---------------------------------------------------------------------------
# The depth check already existed, but in only one place and on only one path:
# `.github/workflows/hopper.yml` hardcoded `MAX_QUEUE: "80"` and stood down
# above it. Hopper is the OFF-SCHEDULE REFILL path — the one workflow whose
# entire job is to ADD work when the queue is starving. Meanwhile each of the
# eleven producer scans carries its own `schedule: cron` and calls the reusable
# core `.github/workflows/_claude-scan.yml` directly, and that core had zero
# depth gating. So the one path that could only ever fire when the queue was
# nearly empty was throttled, and the eleven that fire on a timer regardless
# were not.
#
# That asymmetry is the bug. Measured on 2026-08-14: 311 open issues, 158 of
# them labelled `agent-ready`, while a "max 80" guard sat in the repository.
# Past the cap, issues go stale before anyone can work them, so filing more is
# not productivity — it is landfill.
#
# What is bounded here is what the automated producers ADD: the `agent-ready`
# population, which is the same one hopper already measured, so the two numbers
# keep meaning the same thing. It is NOT a bound on the whole backlog, and
# `agent-ready` is not literally the picker's input set — `pick-next.sh:57`
# defaults RALPH_REQUIRE_LABELS to empty and `ralph-tick.md:479` calls it with
# no override, so the live loop draws from every open issue minus its exclude
# set. Stating that plainly here so a later reader does not conclude Ralph's
# real queue is capped at 90.
#
# ---------------------------------------------------------------------------
# WHY IT LIVES IN scripts/ralph/ RATHER THAN BESIDE THE WORKFLOWS
# ---------------------------------------------------------------------------
#   * `.github/workflows/ralph-recap-tests.yml:110` lints every
#     `scripts/ralph/*.sh` with `--severity=warning` in CI. No CI job does the
#     same for a script parked under `.github/`, and this file parses two
#     untrusted-ish values (a dispatch input and a `gh` payload).
#     (Careful with the wording above: a comment line whose first word is the
#     linter's own name is read as a directive, not as prose — SC1073.)
#   * `scripts/ralph/test_exec_bits.sh` fails any `scripts/ralph/*.sh` whose
#     RECORDED git mode is not 100755. Both callers invoke this file by bare
#     path, where a dropped exec bit is a "permission denied" that only ever
#     surfaces on a scheduled run nobody is watching.
#   * `scripts/ralph/pick-next.sh` already reads this same `agent-ready`
#     population. How deep that queue is allowed to get is Ralph-loop backlog
#     policy, not workflow plumbing.
#   * Neither caller could host it. `_claude-scan.yml` and `hopper.yml` must
#     apply the SAME ceiling to the SAME population; a copy inside each is
#     precisely the duplication this file exists to remove.
#
# ---------------------------------------------------------------------------
# A FULL QUEUE EXITS 0. AN UNMEASURABLE QUEUE EXITS 1.
# ---------------------------------------------------------------------------
# Standing down is the normal, expected outcome on a healthy-but-deep backlog,
# and it is reported as a SUCCESS: a scheduled workflow that goes red every
# night is a workflow everyone learns to ignore, and then nobody notices the
# night it goes red for a real reason.
#
# A measurement failure is the opposite — a genuine error, reported as one.
# Both silent alternatives are worse. Failing OPEN would file issues into a
# queue nobody measured, which is the exact outcome the ceiling exists to
# prevent. Failing CLOSED-and-quiet would disable every producer indefinitely
# on a broken token, behind a warning nobody reads. So an unusable ceiling, a
# non-zero `gh`, or a depth that is not a plain integer all print `::error::`
# and exit 1, and no `proceed` output is written at all.
#
# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------
#   BACKLOG_CEILING      Per-dispatch override of the ceiling, forwarded from
#                        the `backlog_ceiling` workflow input. UNSET or EMPTY
#                        means "use the default below" — an unset
#                        workflow_dispatch input arrives as the empty string,
#                        so empty must mean the default and never "no ceiling".
#                        Anything else that is not a positive integer is a
#                        typo or an attack and is a hard error; it must never
#                        silently revert to the default, or an operator who
#                        typed `9O` believes the cap was raised when it was
#                        not.
#   GITHUB_REPOSITORY    owner/repo to count in. When non-empty it is passed
#                        as `--repo`, so the answer never depends on which
#                        directory the runner happens to sit in.
#   GH_TOKEN             Read by `gh`; the callers bind it to `github.token`.
#   GITHUB_OUTPUT        Step-output file. Optional: unset is tolerated so the
#                        script is runnable by hand.
#   GITHUB_STEP_SUMMARY  Job-summary file. Optional, same reason.
#
# Exit codes:
#   0 — a verdict was reached (proceed OR stand down; both are successes)
#   1 — the ceiling or the depth could not be trusted, so there is no verdict
set -euo pipefail

# THE SINGLE SOURCE OF TRUTH for how deep the agent-ready queue may get.
#
# It is stated here and nowhere else on purpose. `hopper.yml` and
# `_claude-scan.yml` must agree about when the queue is full: if they disagree,
# one path throttles while the other keeps filing, which is exactly how a
# `MAX_QUEUE: "80"` in hopper coexisted with a backlog of 158 filed by the
# scans. Two hardcoded numbers that happen to agree today diverge tomorrow, so
# both callers read this constant by calling this script — neither one carries
# a ceiling of its own. `creek-tools/tests/test_backlog_ceiling_gate.py`
# asserts that this assignment appears exactly once and that no workflow
# declares a numeric ceiling beside it.
readonly MAX_AGENT_READY_QUEUE=90

# Charset guards, in the same spirit as `_claude-scan.yml`'s `scan_name` guard
# (`_claude-scan.yml:66-73`): GitHub Actions inputs have no `pattern:` field,
# so a value crossing from a workflow_dispatch input into this shell is
# validated here, on an allowlist, BEFORE it is used for anything. The ceiling
# never reaches an arithmetic or `eval` context until it has matched, so
# `90; touch /tmp/pwned` is rejected as a malformed number rather than
# evaluated as source text on a runner holding `issues: write`.
readonly POSITIVE_INTEGER_RE='^[1-9][0-9]*$'
# The depth may legitimately be zero, so it admits `0` where the ceiling
# pattern does not. It is otherwise the SAME shape on purpose — no leading
# zeros. `gh` exits 0 while printing `null` when a `--jq` filter misses, and
# `null` must be an error rather than something a comparison quietly reads as
# "the queue is empty, file away". Leading zeros are excluded for a second
# reason: `[ "$count" -ge … ]` parses an operand like `011` as OCTAL, and an
# invalid one like `019` makes `[` itself fail — which, inside an `if`, is a
# silent slide into the `else` branch (proceed=true) rather than a loud error.
# `jq length` cannot emit that, so this is defence in depth, not a live hole;
# the two allowlists are kept symmetric so nobody has to work out which one is
# the lenient one.
readonly NON_NEGATIVE_INTEGER_RE='^(0|[1-9][0-9]*)$'

# `--limit 300` saturates far above any plausible ceiling, so even a saturated
# count still yields the correct verdict: if 300 issues came back, the queue is
# unambiguously over any cap anyone would set, and the stand-down is right
# whether or not the true depth is 300 or 3000. It matches the limit
# `pick-next.sh` uses on the same population.
readonly DEPTH_QUERY_LIMIT=300

# `::error::` on STDOUT, matching `_claude-scan.yml`'s own guards: that is the
# stream the Actions runner reads workflow commands from, so the annotation
# lands on the run rather than only in the raw log.
fail() { # fail <message>
  printf '::error::backlog-gate: %s\n' "$1"
  exit 1
}

# Optional writes, deliberately as `if` blocks rather than `[ -n "$X" ] && …`
# chains: under `set -e` a function whose LAST command is a false `&&` chain
# returns 1 and kills the script, so the "no $GITHUB_OUTPUT" path would abort
# the run instead of printing a verdict.
write_outputs() { # write_outputs <key=value…>
  if [ -n "${GITHUB_OUTPUT:-}" ]; then
    printf '%s\n' "$@" >>"$GITHUB_OUTPUT"
  fi
}

# EXACTLY ONE line, to stdout and to the job summary. One line, naming both the
# measured depth and the ceiling in force, is what lets a reader tell a working
# gate from a stuck one at a glance — and keeps a summary that eleven scans
# append to from becoming noise.
report() { # report <line>
  printf '%s\n' "$1"
  if [ -n "${GITHUB_STEP_SUMMARY:-}" ]; then
    printf '%s\n' "$1" >>"$GITHUB_STEP_SUMMARY"
  fi
}

# --- the ceiling in force ---------------------------------------------------
ceiling="$MAX_AGENT_READY_QUEUE"
override="${BACKLOG_CEILING:-}"
if [ -n "$override" ]; then
  if [[ ! "$override" =~ $POSITIVE_INTEGER_RE ]]; then
    fail "BACKLOG_CEILING must be a positive integer, got '$override' — refusing to fall back to the default, because a silent fallback tells an operator the cap was raised when it was not"
  fi
  ceiling="$override"
fi

# --- the one depth query ----------------------------------------------------
# Built as an array so no argument can be word-split, and so `--repo` is either
# a real pair of tokens or absent entirely (an empty `--repo ""` is an error to
# gh, not a no-op).
depth_query=(issue list --label agent-ready --state open
  --limit "$DEPTH_QUERY_LIMIT" --json number --jq length)
if [ -n "${GITHUB_REPOSITORY:-}" ]; then
  depth_query+=(--repo "$GITHUB_REPOSITORY")
fi

# The assignment sits in an `if` condition so `set -e` cannot kill the script
# before it can say WHY the queue is unmeasurable.
count=""
if ! count="$(gh "${depth_query[@]}")"; then
  fail "could not measure the agent-ready backlog (gh exited non-zero); filing into an unmeasured queue is the outcome the ceiling exists to prevent"
fi
if [[ ! "$count" =~ $NON_NEGATIVE_INTEGER_RE ]]; then
  fail "the agent-ready depth query returned '$count', which is not a count; a run that cannot measure the queue must not guess at it"
fi

# --- the verdict ------------------------------------------------------------
# `-ge`, not `-gt`: a queue sitting exactly ON the cap is full, matching the
# guard hopper used to carry. An off-by-one in a throttle is invisible until it
# is a trend.
if [ "$count" -ge "$ceiling" ]; then
  proceed=false
  printf -v verdict \
    'Backlog gate: %s open agent-ready issues, at or above the ceiling of %s — standing down on purpose so the queue drains. A deliberate no-op, not an error.' \
    "$count" "$ceiling"
else
  proceed=true
  printf -v verdict \
    'Backlog gate: %s open agent-ready issues, below the ceiling of %s — proceeding.' \
    "$count" "$ceiling"
fi

write_outputs "count=$count" "ceiling=$ceiling" "proceed=$proceed"
report "$verdict"
