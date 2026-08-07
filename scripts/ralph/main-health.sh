#!/usr/bin/env bash
# scripts/ralph/main-health.sh
#
# Is `main` itself green? Prints exactly ONE token on stdout and exits 0 — the
# same query contract as pr-ready.sh, so a caller can write
# `tok="$(main-health.sh)"` and compare it directly. A non-zero exit (2) is a
# usage/tooling error, NEVER a verdict about `main`.
#
#   green    the newest CONCLUSIVE ci.yml run on `main` succeeded
#   red      the newest conclusive run failed / timed out / failed at startup
#   pending  nothing has concluded in the window, but a run is in flight
#   unknown  we could not read an answer we are willing to act on
#
# ---------------------------------------------------------------------------
# WHY THIS EXISTS (issue #1159)
# ---------------------------------------------------------------------------
# PR #1157 replaced a strict pre-merge freshness gate with a RISK-BASED one: a
# lane that is behind `main` still merges when the two changesets provably
# cannot interact (pr-ready.sh's RISK_SURFACE_RE plus merge-base disjointness).
# That relaxation bought back everything the strict rule cost — measured across
# #1022 landing, requiring `behind_by == 0` outright drove CI runs per PR from
# 1.00 to 1.61 (max 5) and p90 PR latency from 15 to 104 minutes (#1137).
#
# It rests on exactly one sentence of justification, written into pr-ready.sh's
# own header: "What backstops the residual risk is the full CI run on
# `push: main` — every squash-merge re-proves the merged result, so a stale
# green that slips through is caught on `main` rather than assumed away."
#
# Nothing in the loop had ever READ that run's conclusion. The backstop was a
# premise, not a check — and while `main` is red the premise is simply false:
# the "it gets caught on `main`" half of the argument is not happening, so the
# loop goes on stacking unvalidated merges onto a broken tree, burying the
# culprit under the commits that follow it, and then reads each lane's
# inherited CI failure as that lane's own fault and dispatches a fix worker at
# a failure the lane never caused.
#
# This script is that premise made checkable. pr-ready.sh spends it at the one
# point the relaxation is actually invoked — see its "WHY IT IS A PRECONDITION,
# NOT A GATE (#1159)" block. It is deliberately NOT a gate on merging: a lane
# with `behind_by == 0` never invokes the relaxation, never asks this question,
# and merges even while `main` is red. That is the shape of the PR that FIXES
# `main`, so the remedy needs no bypass label, and the deadlock is closed by
# construction — for breakage that is a function of the merged tree: a
# `behind_by == 0` lane's own CI ran on a ref that already contains whatever
# broke `main`, under the identical push/PR job matrix (see pr-ready.sh's
# block for the full ancestry argument).
#
# It is NOT closed for breakage that is not tree-borne. A `behind_by == 0`
# lane whose CI went green BEFORE a dependency advisory published, a pin
# expired, or a package was yanked carries the identical pinned state and
# still reads `ready` — its green is stale with respect to that class even
# though the tree never changed. See the blame-range note below
# (PYSEC-2026-3552) for the observed instance. That residual is accepted, not
# disproven: gating this lane on `main-health.sh` too would make the fix PR
# itself unmergeable while `main` is red, which is the exact deadlock this
# design exists to avoid, and escaping it would need the bypass label the
# design deliberately does not have.
#
# ---------------------------------------------------------------------------
# CIRCUIT BREAKER, NOT BARRIER: the newest CONCLUSIVE run, not the newest run
# ---------------------------------------------------------------------------
# `main` CI lags each merge by ~14 minutes, so at almost any moment on a busy
# fleet the NEWEST run on `main` is still in flight. That is the steady state,
# not an exception. Keying this gate off the newest run would hold every behind
# lane for a full CI round after every single merge — precisely the
# serialization #1138 removed and #1157 exists to keep removed (issue #1159
# constraint 4). Keying off the newest run that actually CONCLUDED means the
# gate trips only on proven breakage: a circuit breaker, not a barrier.
#
# For the same reason a run that was cancelled, skipped, neutral,
# action_required or stale — or whose conclusion field is empty — is NOT
# evidence and the walk keeps going past it. "Somebody cancelled a run" says
# nothing about whether the tree builds; reading it as "not red" would clear
# the gate on no evidence, and reading it as "not green" would wedge every
# behind lane the first time a run gets cancelled.
#
# ---------------------------------------------------------------------------
# FAIL CLOSED: an unreadable answer is not permission
# ---------------------------------------------------------------------------
# Every failure path lands on a token that is never `green`: an empty run list,
# a non-zero `gh`, output we cannot fully parse, a window of nothing but
# inconclusive runs. Almost always that token is `unknown`. The one exception is
# a payload that turns unparseable BELOW a run that a well-formed line already
# proved `red`: that keeps its `red`, because discarding proof we already hold
# is lossy rather than safe (see the malformed-line note in the walk). Either
# way the caller treats anything other than `green` as "hold this lane", whose
# remedy (wait one wake) is always safe; a false `green` merges a second
# unvalidated change onto a tree that is already broken, and that is
# near-unrecoverable — it buries the culprit.
#
# ---------------------------------------------------------------------------
# SCOPE: `ci.yml` only, on the `main` branch only
# ---------------------------------------------------------------------------
# The backstop #1157 leans on is one specific run: the full job matrix that
# `.github/workflows/ci.yml` runs on `push: main`. This script reads that
# workflow and nothing else on purpose. A red `scan-*` audit, a flaky
# `graph-update`, a docs workflow — none of them re-prove the merged result, so
# none of them may stop the fleet from merging. Widening `--workflow` here
# would silently convert this circuit breaker into a repo-wide merge freeze.
#
# ---------------------------------------------------------------------------
# ATTRIBUTION GOES TO STDERR, AND ONLY ON `red`
# ---------------------------------------------------------------------------
# stdout is the token and nothing else, always, or every caller's `[[ "$tok" ==
# green ]]` comparison goes false at once and the whole fleet holds. So the
# explanation lives on stderr, and the healthy path says NOTHING at all.
#
# On `red` stderr carries the failing run's id, url and headSha AND a blame
# RANGE, because the issue asks for the commit that broke `main` and the newest
# red run is not it: merges land minutes apart while a CI round takes ~14, so
# by the time the first red run reports, two or three more commits are already
# on `main` and several of their runs are red too. All that is actually proven
# is that the tree was good at the newest green run's sha and bad at the red
# one's, so `<newest green sha>..<red sha>` is the honest answer. When no green
# run was found — because the window holds none, or because the walk stopped at
# a line it could not parse before reaching one — there is no lower bound and
# therefore no honest range: say the culprit is unattributable rather than
# fabricating one, which would point a human at every commit in the repo's
# history and read, to a script, exactly like a real answer. The red itself is
# still fully attributed either way; it is only the floor that is missing.
#
# The range is a WINDOW, not an accusation, and the wording says so. `main` can
# go red with no culprit commit at all: this gate's own first live run caught
# `main` red on a newly published advisory (PYSEC-2026-3552, cryptography
# 49.0.0), which turned `pip-audit` red on a tree nobody had touched — the two
# runs bounding the range differed by a commit that was entirely innocent.
# Expired pins, rotated credentials and upstream yanks all break `main` the same
# way. So report what is actually known — good at the green sha, bad at the red
# one — and name the time-triggered possibility, rather than telling a debugging
# worker to go read a diff that does not contain the bug.
#
# COST: exactly ONE `gh` call on every path, including every failure path. This
# runs per behind lane per wake across the whole fleet; a retry loop here is a
# rate limit there.
#
# Usage:  main-health.sh [--repo <owner/repo>]
set -euo pipefail

# The workflow that IS the backstop, and the branch it backstops. See the SCOPE
# note above before widening either.
readonly CI_WORKFLOW="ci.yml"
readonly MAIN_BRANCH="main"

# How many runs back to look. Big enough that a cluster of cancelled runs (a
# push storm cancelling nothing on `main`, a re-run) cannot hide the last
# conclusive answer, and big enough to still hold a green below a run of reds
# so the blame range has a floor. Small enough to stay one cheap request.
readonly RUN_WINDOW=20

# One `gh run list` call, extracted server-side into one line per run, NEWEST
# FIRST, five `|`-separated fields: status|conclusion|headSha|databaseId|url.
# `// ""` on every field because `conclusion` is JSON null while a run is in
# flight, and `| tostring` on `databaseId` because it is a NUMBER — without it
# `join("|")` errors, jq exits non-zero, and gh reports a failure we would then
# (correctly, but pointlessly) read as `unknown`.
readonly RUN_FIELDS="status,conclusion,headSha,databaseId,url"
readonly RUN_JQ='.[] | [(.status // ""), (.conclusion // ""), (.headSha // ""), ((.databaseId // "") | tostring), (.url // "")] | join("|")'

# The one status that means a run has something to say about whether the tree
# builds. Everything else is either in flight (below) or not evidence.
readonly STATUS_COMPLETED="completed"

# Statuses that mean "evidence is coming". GitHub reports `waiting`,
# `requested` and `pending` for approval- and deployment-gated runs alongside
# the two common ones; all of them are a run that has not concluded yet.
readonly -a IN_FLIGHT_STATUSES=(queued in_progress waiting requested pending)

# The conclusions that ARE evidence. `timed_out` and `startup_failure` count as
# failures because a run that ran out of wall clock or died before its first
# step never proved the tree builds — and noticing that is the backstop's whole
# job. Every other conclusion (cancelled / skipped / neutral / action_required /
# stale / empty) is not evidence; see the circuit-breaker note above.
readonly SUCCESS_CONCLUSION="success"
readonly -a FAILING_CONCLUSIONS=(failure timed_out startup_failure)

readonly TOKEN_GREEN="green"
readonly TOKEN_RED="red"
readonly TOKEN_PENDING="pending"
readonly TOKEN_UNKNOWN="unknown"

die()  { echo "main-health: $1" >&2; exit 2; }
warn() { echo "main-health: $1" >&2; }

# True when <needle> is one of the remaining arguments. Written as an explicit
# `if` rather than `[[ … ]] && return 0` so it carries no `set -e` hazard of its
# own, whatever context a future caller invokes it from.
in_list() { # in_list <needle> <candidate…>
  local needle="$1" item
  shift
  for item in "$@"; do
    if [[ "$needle" == "$item" ]]; then return 0; fi
  done
  return 1
}

# --- arguments: one optional flag, and NO positional arguments --------------
# This script asks about `main`, not about a PR. A stray number is a caller
# confusing it with pr-ready.sh, and silently ignoring it would answer a
# question nobody asked.
repo_args=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo) [[ $# -ge 2 ]] || die "--repo needs a value"; repo_args+=(--repo "$2"); shift 2 ;;
    -*)     die "unknown option: $1" ;;
    *)      die "unexpected argument: '$1' (usage: main-health.sh [--repo <owner/repo>])" ;;
  esac
done

# --- the one call -----------------------------------------------------------
# `${arr[@]+"${arr[@]}"}` expands to nothing when the array is empty instead of
# tripping `set -u` on bash 3.2 (stock /bin/bash on macOS), exactly as
# pr-ready.sh does at its own `repo_args` expansion. No `--repo` is invented
# when none was given: gh then resolves the repo from the cwd, which is what
# every direct invocation wants.
#
# The assignment sits in an `if` condition so `set -e` cannot kill us before we
# print a token — and so gh printing a happy answer AND exiting non-zero (the
# dangerous shape: the output is already on stdout) still fails closed.
runs=""
if ! runs="$(gh run list --workflow "$CI_WORKFLOW" --branch "$MAIN_BRANCH" \
    --limit "$RUN_WINDOW" --json "$RUN_FIELDS" --jq "$RUN_JQ" \
    ${repo_args[@]+"${repo_args[@]}"} 2>/dev/null)"; then
  warn "could not list $CI_WORKFLOW runs on $MAIN_BRANCH (gh exited non-zero)"
  printf '%s\n' "$TOKEN_UNKNOWN"
  exit 0
fi

if [[ -z "$runs" ]]; then
  warn "no $CI_WORKFLOW runs on $MAIN_BRANCH in the last $RUN_WINDOW"
  printf '%s\n' "$TOKEN_UNKNOWN"
  exit 0
fi

# --- the walk: newest first, stop at the first CONCLUSIVE run ---------------
verdict=""          # what the newest conclusive run decided, once we find it
red_conclusion=""   # attribution for a red verdict …
red_sha=""
red_id=""
red_url=""
green_sha=""        # newest success BELOW that red run — the blame range floor
in_flight_seen=""   # something is running, so evidence is on its way
malformed=""        # a line we could not fully parse; stops the walk there

while IFS= read -r line; do
  # Blank lines carry no run (the herestring always appends one at the end).
  [[ -n "$line" ]] || continue

  IFS='|' read -r status conclusion sha run_id url rest <<<"$line"

  # Split by FIELD COUNT, never by seeking a separator — the same rule and the
  # same reason as pr-ready.sh:323 / :370 / :483. A status enum, a conclusion
  # enum, a sha, an integer id and a URL can none of them contain a `|`, so a
  # surplus 6th field means a `|` appeared where none legitimately can and the
  # fields may have shifted under us; a missing one means the payload is not
  # the shape we asked for. `conclusion` is exempt: it is legitimately empty
  # while a run is in flight.
  #
  # DELIBERATE STRICTNESS: a malformed line STOPS THE WALK — it does NOT skip
  # the line and keep going. Skipping would be fail-OPEN: a malformed line
  # sitting above a legitimate green one would silently resolve to `green`,
  # which is the one answer that lets a merge through. A payload we cannot
  # fully parse is a payload we do not trust. The suite pins that with a
  # deliberately GREEN-SHAPED malformed line carrying a surplus 6th field, so
  # do not "simplify" this into a `continue`.
  #
  # What stopping RESOLVES TO is decided after the loop, and it is not always
  # `unknown` (#1159). Stopping is ALL a malformed line does: it never CREATES
  # a verdict and never UPGRADES one, so the only answer it can ever produce on
  # its own is `unknown` — but a `red` that a WELL-FORMED line above it already
  # proved survives it. Past a decided red the walk is no longer deciding
  # anything; it is only hunting older runs for the newest green, the blame
  # range's floor. Dropping the proven red to `unknown` there is lossy, not
  # safe: it costs nothing in merge safety (pr-ready.sh holds a lane the same
  # on both) and silently disables ralph-tick Step 0b's `ci-debugging` dispatch,
  # which fires only on the literal `red`, exactly when `main` is broken. That
  # is a refinement of this strictness, not a loosening of it.
  if [[ -n "$rest" || -z "$status" || -z "$sha" || -z "$run_id" || -z "$url" ]]; then
    malformed="$line"
    break
  fi

  if in_list "$status" "${IN_FLIGHT_STATUSES[@]}"; then
    in_flight_seen=1
    continue
  fi

  # Anything that is neither in flight nor completed is a status we do not
  # recognise — GitHub adds values over time. Treat it as no evidence and keep
  # walking rather than aborting: an unrecognised status must not be able to
  # freeze the fleet, and with no conclusive run behind it the walk still ends
  # on `unknown` anyway.
  [[ "$status" == "$STATUS_COMPLETED" ]] || continue

  if [[ "$conclusion" == "$SUCCESS_CONCLUSION" ]]; then
    if [[ -z "$verdict" ]]; then
      # The newest conclusive run is a success: `main` builds.
      verdict="$TOKEN_GREEN"
    else
      # We already have a red verdict above this run, so this success is the
      # floor of the blame range — the last sha `main` was provably good at.
      green_sha="$sha"
    fi
    break
  fi

  if in_list "$conclusion" "${FAILING_CONCLUSIONS[@]}"; then
    if [[ -z "$verdict" ]]; then
      verdict="$TOKEN_RED"
      red_conclusion="$conclusion"
      red_sha="$sha"
      red_id="$run_id"
      red_url="$url"
    fi
    # Keep walking even once red is decided: the older runs are where the
    # newest green — the blame range's floor — lives.
    continue
  fi

  # cancelled / skipped / neutral / action_required / stale / empty: not
  # evidence about whether the tree builds. Skip and keep walking.
done <<<"$runs"

# A malformed line only ever stopped the walk; what that stop means depends
# entirely on what the walk had ALREADY PROVEN when it hit the line, and the
# proof only ever came from well-formed lines above it.
#
# Nothing decided yet ⇒ `unknown`. Everything that could still have decided the
# answer lies behind a line we have just refused to trust, so there is no
# answer to give. This is the fail-closed case, and it is the whole reason the
# malformed check is not a `continue`.
#
# Already `red` ⇒ fall through and keep it (see the walk's note). The condition
# is written against `red` specifically rather than "any non-empty verdict" so
# it can only ever PRESERVE the one verdict that still holds the lane: were a
# future edit to let `green` be decided before a malformed line, that green
# would be discarded here rather than handed out as permission to merge.
if [[ -n "$malformed" && "$verdict" != "$TOKEN_RED" ]]; then
  warn "unparseable run-list line ('$malformed'); refusing to classify $MAIN_BRANCH from a payload we cannot fully parse"
  printf '%s\n' "$TOKEN_UNKNOWN"
  exit 0
fi

case "$verdict" in
  "$TOKEN_GREEN")
    # The healthy path says nothing at all — see the attribution note above.
    printf '%s\n' "$TOKEN_GREEN"
    exit 0 ;;
  "$TOKEN_RED")
    warn "$MAIN_BRANCH is RED: $CI_WORKFLOW run $red_id concluded $red_conclusion at $red_sha ($red_url)"
    if [[ -n "$green_sha" ]]; then
      warn "blame range ${green_sha}..${red_sha} — ${MAIN_BRANCH} was proven good at the first and bad at the second; the newest red run is not necessarily the one that broke it, and a break with no culprit commit at all (a newly published advisory, an expired pin, a rotated credential) lands in this same range"
    elif [[ -n "$malformed" ]]; then
      # The walk stopped short, so `green_sha` is empty for want of READING a
      # green rather than for want of one existing — same missing floor, but a
      # different reason, and saying "no green run in the window" here would be
      # a claim we never checked. The floor is not fabricated from the line we
      # refused to trust, nor from anything below it that we never reached.
      warn "the walk stopped at an unparseable run-list line ('$malformed') below that failing run, so no green run was ever read and the culprit commit is unattributable from what we could parse — the range's floor, if any, is in the unread remainder of the window; re-run once gh returns a payload we can parse, or bisect $MAIN_BRANCH by hand"
    else
      warn "no green run in the last $RUN_WINDOW $CI_WORKFLOW runs on $MAIN_BRANCH, so the culprit commit is unattributable from this window — widen the window or bisect $MAIN_BRANCH by hand"
    fi
    printf '%s\n' "$TOKEN_RED"
    exit 0 ;;
esac

if [[ -n "$in_flight_seen" ]]; then
  # Nothing has concluded, but something is running: evidence is coming, and
  # waiting one wake is genuinely the right answer.
  printf '%s\n' "$TOKEN_PENDING"
  exit 0
fi

warn "the last $RUN_WINDOW $CI_WORKFLOW runs on $MAIN_BRANCH carry no conclusive result and none is in flight"
printf '%s\n' "$TOKEN_UNKNOWN"
