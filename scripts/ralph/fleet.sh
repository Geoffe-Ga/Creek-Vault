#!/usr/bin/env bash
# scripts/ralph/fleet.sh
#
# Worktree fleet manager for the parallel Ralph loop (Geoffe-Ga/Creek-Vault).
#
# Ralph's outer loop can work several *parallelizable* backlog issues at once,
# each in its own git worktree so concurrent edits never collide on disk. This
# script is the mechanism the orchestrator (`.claude/commands/ralph-tick.md`)
# and the worker agent (`.claude/agents/ralph-worker.md`) use to create,
# inspect, sync, and tear down those worktrees — never more than
# `max_workers` (default 4) at a time.
#
# Design & concurrency model: scripts/ralph/FLEET.md
#
# Design contract ("optimistic parallelism, pessimistic merge"):
#   * Parallel work is a speculation that the chosen issues are independent.
#   * Correctness is guaranteed at MERGE time, not pick time: the orchestrator
#     merges ONE PR per tick, then merges `origin/main` into every surviving
#     worktree and re-runs its local gate before that worktree may merge.
#   * A worktree with a merge conflict drops to Gate 1 (see the docs in
#     `scripts/ralph/FLEET.md`). Nothing here weakens a gate.
#
# Worktrees live under `.ralph/worktrees/issue-<N>` on branch
# `issue/<N>-<slug>`. The issue number is the primary key; there is no separate
# slot bookkeeping. The `.ralph/` directory is git-ignored.
#
# Config is read from `scripts/ralph/state.json`:
#   max_workers       Maximum concurrent worktrees (default 4).
#   parallel_enabled  When false, `free` reports at most 1 (sequential Ralph).
#
# Subcommands:
#   list             Print active worktrees, one per line:
#                      <issue>\t<branch>\t<path>
#   active           Print just the active issue numbers, space-separated.
#   count            Print the number of active worktrees.
#   free             Print how many more workers may be started right now.
#   path <N>         Print the worktree path for issue N (empty + exit 1 if none).
#   assign <N> <slug>  Create (or reuse) a worktree for issue N; prints its
#                      absolute path. Prefers, in order: an existing LOCAL
#                      branch, then a branch of that name on `origin` (a lane
#                      `release` deleted locally while its PR lived on — #1180),
#                      and only then a fresh branch off origin/main. Aborts if
#                      the remote lookup is unreadable, and refuses if the fleet
#                      is full.
#   adopt <N> <PR>   The bot-PR variant of assign: create (or reuse) a worktree
#                      for issue N attached to PR's EXISTING head branch (e.g.
#                      Dependabot's), so fixes push to that branch instead of
#                      opening a second PR. Prints its absolute path. Refuses a
#                      fork PR (its branch is not pushable) and a full fleet.
#   sync <N>         Integrate the latest origin/main into issue N's worktree
#                      branch by MERGE (no history rewrite ⇒ a plain push updates
#                      the PR; no force-push, ever). Exit 0 clean, exit 3 on
#                      conflict (merge aborted, worktree left clean).
#   release <N>      Remove issue N's worktree and delete its LOCAL branch. The
#                      remote branch is never touched, so a released lane can be
#                      re-assigned later and reattaches to it (see assign).
#   reconcile        Release worktrees whose PR merged/closed or whose issue is
#                      closed, then `git worktree prune`. Needs the gh CLI.
#
# Exit codes: 0 ok · 1 usage/not-found · 2 tooling missing · 3 merge conflict.
set -euo pipefail

readonly DEFAULT_MAX_WORKERS=4
readonly WORKTREE_ROOT=".ralph/worktrees"
readonly STATE_FILE="scripts/ralph/state.json"

# Print an actionable error and exit. Callers pass the message as ONE arg and an
# optional exit code as the second ($2) — so the code never leaks into the
# message. Defaults to exit 1 for the common single-arg callers.
die() {
  echo "fleet: $1" >&2
  exit "${2:-1}"
}

# Resolve the MAIN worktree's root, so the script works from any worktree/subdir.
# `git rev-parse --show-toplevel` cannot do this: it answers with whatever worktree
# you are STANDING IN, and every lane path would then be computed relative to
# another lane. Observed live from inside `.ralph/worktrees/issue-1016` with two
# lanes active: `list` printed nothing and `free` said 4, so the orchestrator would
# start workers past max_workers, and `sync` died with "no worktree for issue N" —
# yet an adopted worker's FIRST action is `fleet.sh sync` from inside its own
# worktree. `git worktree list --porcelain` always prints the main worktree first,
# and that is the one holding `.ralph/`. The awk keeps reading after the match
# (rather than `exit`ing on it) so git never writes into a closed pipe.
repo_root() {
  local main
  main="$(git worktree list --porcelain 2>/dev/null |
    awk '/^worktree / && !seen { print substr($0, 10); seen = 1 }')" || true
  [[ -n "$main" ]] || die "not inside a git repository"
  printf '%s\n' "$main"
}

# Read an integer/bool field from state.json with a fallback. Pure-python so we
# never depend on jq being present for config (gh already needs jq, but config
# reads happen even in offline tests).
state_get() {
  local key="$1" default="$2" file
  file="$(repo_root)/$STATE_FILE"
  if [[ ! -f "$file" ]]; then
    printf '%s\n' "$default"
    return 0
  fi
  python3 - "$file" "$key" "$default" <<'PY'
import json
import sys

path, key, default = sys.argv[1], sys.argv[2], sys.argv[3]
try:
    with open(path, encoding="utf-8") as handle:
        data = json.load(handle)
    value = data.get(key, default)
except (OSError, ValueError):
    value = default
if isinstance(value, bool):
    value = "true" if value else "false"
print(value)
PY
}

max_workers() {
  local raw
  raw="$(state_get max_workers "$DEFAULT_MAX_WORKERS")"
  [[ "$raw" =~ ^[0-9]+$ ]] || raw="$DEFAULT_MAX_WORKERS"
  printf '%s\n' "$raw"
}

parallel_enabled() {
  [[ "$(state_get parallel_enabled true)" == "true" ]]
}

# Absolute path of the worktree directory for an issue (may not exist yet).
issue_dir() {
  printf '%s/%s/issue-%s\n' "$(repo_root)" "$WORKTREE_ROOT" "$1"
}

# Emit "<issue>\t<branch>\t<path>" for every active Ralph worktree, sorted by
# issue number. Derived from live git state — never from stored bookkeeping —
# so the loop stays re-entrant.
list_worktrees() {
  local root
  root="$(repo_root)"
  git -C "$root" worktree list --porcelain | awk -v root="$root/$WORKTREE_ROOT/issue-" '
    /^worktree /   { path = substr($0, 10) }
    /^branch /     { branch = substr($0, 8); sub(/^refs\/heads\//, "", branch) }
    /^$/           { emit() }
    END            { emit() }
    function emit() {
      if (path != "" && index(path, root) == 1) {
        issue = substr(path, length(root) + 1)
        sub(/\/.*/, "", issue)
        printf "%s\t%s\t%s\n", issue, branch, path
      }
      path = ""; branch = ""
    }
  ' | sort -n
}

count_active() {
  list_worktrees | grep -c . || true
}

cmd_list() {
  list_worktrees
}

cmd_active() {
  list_worktrees | cut -f1 | paste -sd' ' -
}

cmd_count() {
  count_active
}

cmd_free() {
  local cap active free
  cap="$(max_workers)"
  parallel_enabled || cap=1
  active="$(count_active)"
  free=$((cap - active))
  ((free < 0)) && free=0
  printf '%s\n' "$free"
}

cmd_path() {
  local issue="$1" dir
  [[ -n "$issue" ]] || die "path: missing issue number"
  dir="$(issue_dir "$issue")"
  if [[ -d "$dir" ]]; then
    printf '%s\n' "$dir"
  else
    exit 1
  fi
}

# Acquire the assign critical-section lock via an atomic `mkdir` (no flock, so
# macOS/bash-3.2-safe), bounded by FLEET_LOCK_TIMEOUT seconds (default 10). On
# success installs an EXIT trap that releases the lock on EVERY later exit path —
# the success return, a cap-refused `die`, or a fetch/worktree failure — because
# die() calls `exit`, which fires an EXIT (not RETURN) trap. On timeout it dies
# non-zero with an actionable stale-lock hint (a held lock is never removed here;
# only its owner or the operator clears it).
acquire_assign_lock() {
  local lock="$1" timeout="${FLEET_LOCK_TIMEOUT:-10}" waited=0
  [[ "$timeout" =~ ^[0-9]+$ ]] || timeout=10
  while true; do
    if mkdir "$lock" 2>/dev/null; then
      trap 'rmdir "$lock" 2>/dev/null || true' EXIT
      return 0
    fi
    ((waited >= timeout)) && break
    sleep 1
    waited=$((waited + 1))
  done
  die "assign: could not acquire lock (stale $lock?); remove it if no assign is running" 1
}

# Does `origin` carry a branch of this name? Prints `found` or `absent`, and
# returns NON-ZERO when the answer is UNREADABLE — a network blip, an expired
# credential, a dead remote. The caller must treat that third outcome as an
# abort, never as `absent`: branching off `main` when a remote branch MAY exist
# is precisely the failure this function was added to prevent (#1180).
#
# `ls-remote` without `--exit-code`, because that flag conflates the two answers
# this function must keep apart: it exits 2 for "no such ref" and non-zero for a
# transport failure, and the caller needs "absent" and "unreadable" to take
# OPPOSITE actions. Empty output with exit 0 is the unambiguous "absent".
#
# The pattern is FULLY QUALIFIED (`refs/heads/<branch>`) and the answer is then
# matched EXACTLY: ls-remote patterns are tail-matched on path components, so a
# bare `issue/9-x` would also be answered by `refs/heads/wip/issue/9-x` — a
# different branch, whose commits are not this PR's. awk with a string compare
# rather than grep, because a branch name is not a regex (`+`, `.` and `[` are
# all legal in a git ref).
remote_branch_state() { # remote_branch_state <root> <branch>
  local root="$1" branch="$2" out
  out="$(git -C "$root" ls-remote --heads origin "refs/heads/$branch" 2>/dev/null)" || return 1
  if awk -v want="refs/heads/$branch" '$2 == want { hit = 1 } END { exit !hit }' <<<"$out"; then
    printf 'found\n'
  else
    printf 'absent\n'
  fi
}

cmd_assign() {
  local issue="$1" slug="${2:-}" root dir branch base lock remote_state
  [[ -n "$issue" ]] || die "assign: usage: assign <issue> <slug>"
  [[ "$issue" =~ ^[0-9]+$ ]] || die "assign: issue must be numeric, got '$issue'"
  root="$(repo_root)"
  dir="$(issue_dir "$issue")"

  # Re-entrant: an existing worktree for this issue is simply reused. Checked
  # BEFORE locking so re-entry never contends on the lock.
  if [[ -d "$dir" ]]; then
    printf '%s\n' "$dir"
    return 0
  fi

  # Serialize the whole check-then-create below: without a lock two concurrent
  # assigns could both pass the cap check and both add a worktree, overflowing
  # max_workers (TOCTOU). Ensure the worktree root exists first so the lock has a
  # home even on a cold fleet.
  mkdir -p "$root/$WORKTREE_ROOT"
  lock="$root/$WORKTREE_ROOT/.assign.lock"
  acquire_assign_lock "$lock"

  # Enforce the cap only when creating a *new* worktree — INSIDE the lock so the
  # count-then-create is atomic. A refused assign calls die → exit, and the EXIT
  # trap installed above still releases the lock.
  if [[ "$(cmd_free)" -le 0 ]]; then
    die "assign: fleet is full ($(count_active)/$(max_workers) workers active)"
  fi

  slug="$(sanitize_slug "$slug")"
  branch="issue/${issue}-${slug}"
  base="origin/main"

  git -C "$root" fetch --quiet origin main || die "assign: could not fetch origin/main"

  if git -C "$root" show-ref --verify --quiet "refs/heads/$branch"; then
    # Branch already exists (prior tick) — attach a worktree to it.
    git -C "$root" worktree add "$dir" "$branch" >&2
  else
    # NO LOCAL REF IS NOT THE SAME AS NO BRANCH (#1180). `cmd_release` below ends
    # with `git branch -D`, so a lane that was released — to free a slot for a
    # hotter issue, or because its PR was stalled — leaves the PR's branch alive
    # on the REMOTE and nothing at all locally. Re-assigning that issue used to
    # fall straight through to the `-b … origin/main` path below and cut a BRAND
    # NEW branch of the same name off `main`: tracking `origin/main`, carrying
    # none of the PR's commits, and indistinguishable from a healthy lane in
    # `fleet.sh list`. `sync` then answers "Already up to date." — of course it
    # does, the lane IS main — and the next push is either rejected
    # (non-fast-forward) or, if somebody reaches for `--force`, destroys every
    # commit on the PR branch. Only the loop's no-force-push rule kept the live
    # occurrence (2026-08-06, PR #1117's lane) from being data loss.
    #
    # FAIL CLOSED on an unreadable answer. The whole defect is "we assumed no
    # branch existed when one did", so an answer we cannot read must abort rather
    # than repeat the assumption. `die` exits, which fires the EXIT trap
    # `acquire_assign_lock` installed, so the lock is released on this path too.
    remote_state="$(remote_branch_state "$root" "$branch")" ||
      die "assign: could not read origin for '$branch'; refusing to guess whether a released lane's branch is still there (branching off main would orphan its PR's commits)"
    if [[ "$remote_state" == "found" ]]; then
      # Explicit refspec, exactly as `cmd_adopt` fetches a bot branch: the
      # tracking ref may not exist yet in this clone, and `--track` needs it.
      # `+` accepts a force-push upstream; it updates only refs/remotes.
      git -C "$root" fetch --quiet origin "+refs/heads/$branch:refs/remotes/origin/$branch" ||
        die "assign: could not fetch origin/$branch"
      # FULLY QUALIFIED start point for the reason cmd_adopt's divergence guard
      # documents: a tag sharing the branch's name outranks the branch in
      # `git rev-parse`'s disambiguation, so an unqualified `origin/$branch`
      # could resolve to a different object than the one intended. `--track`
      # sets the upstream to `origin/$branch` — NOT `origin/main`, which is the
      # wrong-upstream tell the incident report singled out.
      git -C "$root" worktree add --track -b "$branch" "$dir" "refs/remotes/origin/$branch" >&2
    else
      git -C "$root" worktree add "$dir" -b "$branch" "$base" >&2
    fi
  fi

  # Success: release the lock now and clear the trap so a later exit does not try
  # to rmdir an already-gone (or a newly re-acquired) lock.
  rmdir "$lock" 2>/dev/null || true
  trap - EXIT
  printf '%s\n' "$dir"
}

# Attach a lane to a PR's own head branch (Dependabot & friends), so fixes push to
# that PR instead of opening a second one. The local branch name must equal
# headRefName EXACTLY: cmd_reconcile finds lanes with `gh pr list --head
# "$branch"`, so any deviation silently breaks worktree GC.
cmd_adopt() {
  local issue="$1" pr="${2:-}" root dir head_line head_ref is_fork on_branch lock
  [[ -n "$issue" && -n "$pr" ]] || die "adopt: usage: adopt <issue> <pr>"
  # An issue of "abc" would reach issue_dir and create a slot no other subcommand
  # can ever address; a non-numeric PR would reach gh as a search term.
  [[ "$issue" =~ ^[0-9]+$ ]] || die "adopt: issue must be numeric, got '$issue'"
  [[ "$pr" =~ ^[0-9]+$ ]] || die "adopt: PR must be numeric, got '$pr'"
  root="$(repo_root)"
  dir="$(issue_dir "$issue")"

  command -v gh >/dev/null 2>&1 || die "adopt: gh CLI required"
  head_line="$(gh pr view "$pr" --json headRefName,isCrossRepository \
    --jq '(.headRefName // "") + "|" + ((.isCrossRepository // false) | tostring)' \
    2>/dev/null || true)"
  # Split on the LAST separator, not the first: '|' is a legal git branch-name
  # character, so a FORK PR whose head branch is `main-shim|false` answers
  # "main-shim|false|true". Splitting on the first '|' yields head_ref="main-shim"
  # and is_fork="false|true", which never equals "true" — the fork refusal stays
  # silent and the lane attaches to the unrelated base-repo branch `main-shim`,
  # which the worker then pushes to. isCrossRepository is the final field, so the
  # last '|' is the seam.
  head_ref="${head_line%|*}"
  is_fork="${head_line##*|}"
  # gh's `// ""` default emits an empty headRefName for a PR that does not exist;
  # adopting "" would attach the lane to whatever HEAD happens to be.
  [[ -n "$head_ref" ]] || die "adopt: could not resolve the head branch of PR #$pr"
  # A fork's branch lives in another repository — we cannot push fixes to it.
  # Demand the literal "false": any other answer (including a malformed or
  # truncated one, whose fallback name is a real base-repo branch) means the PR's
  # origin is undetermined, which must refuse.
  [[ "$is_fork" == "false" ]] ||
    die "adopt: PR #$pr is cross-repository or its origin is undeterminable; its branch is not pushable"

  # Re-entrant: an existing worktree for this issue is reused — but ONLY if it is
  # already on the PR's head branch. A lane `assign` created sits on
  # `issue/<N>-<slug>`, and silently reusing it would push the fix to a branch the
  # PR does not track: a second PR, and a lane `reconcile` can never find again.
  if [[ -d "$dir" ]]; then
    on_branch="$(git -C "$dir" rev-parse --abbrev-ref HEAD 2>/dev/null || true)"
    [[ "$on_branch" == "$head_ref" ]] ||
      die "adopt: worktree for issue $issue is on '$on_branch', not PR #$pr's '$head_ref' — release it first"
    printf '%s\n' "$dir"
    return 0
  fi

  # Serialize the check-then-create below under the SAME lock `assign` takes.
  # Adopting is a worker start, so it obeys max_workers — and it shares the cap
  # with `assign`, so sharing the count without sharing the lock would leave the
  # TOCTOU that lock exists to close: a concurrent adopt+assign (the ordinary
  # shape of a tick that refills a slot while bridging a bot PR) could both pass
  # the cap check and both add a worktree, overflowing max_workers. Ensure the
  # worktree root exists first so the lock has a home even on a cold fleet.
  mkdir -p "$root/$WORKTREE_ROOT"
  lock="$root/$WORKTREE_ROOT/.assign.lock"
  acquire_assign_lock "$lock"

  # Enforced only when creating a *new* worktree — re-entry above returned before
  # the lock, so an existing lane is never refused and never contends.
  if [[ "$(cmd_free)" -le 0 ]]; then
    die "adopt: fleet is full ($(count_active)/$(max_workers) workers active)"
  fi

  # The bot branch is usually pushed after this clone existed, so fetch the ref
  # itself rather than trust a possibly-absent origin/<ref>. The explicit refspec
  # guarantees the tracking ref used below exists, and its `+` accepts the bot's
  # force-pushes — it updates only refs/remotes, never a local branch.
  git -C "$root" fetch --quiet origin "+refs/heads/$head_ref:refs/remotes/origin/$head_ref" \
    || die "adopt: could not fetch origin/$head_ref"

  if git -C "$root" show-ref --verify --quiet "refs/heads/$head_ref"; then
    # Never reset, never force: a local tip the remote no longer contains is either
    # somebody's unpushed work or a bot force-push, and attaching would build on
    # state that is gone. Both revs are FULLY QUALIFIED because a tag sharing the
    # branch's name outranks the branch in `git rev-parse`'s disambiguation while
    # `git worktree add` picks the branch — unqualified names would vet one object
    # while checking out another.
    git -C "$root" merge-base --is-ancestor \
      "refs/heads/$head_ref" "refs/remotes/origin/$head_ref" \
      || die "adopt: local '$head_ref' diverged from origin/$head_ref — resolve by hand, then re-adopt"
    git -C "$root" worktree add "$dir" "$head_ref" >&2
  else
    git -C "$root" worktree add --track -b "$head_ref" "$dir" "origin/$head_ref" >&2
  fi

  # Success: release the lock now and clear the trap so a later exit does not try
  # to rmdir an already-gone (or a newly re-acquired) lock. Every failure path
  # above still releases it, via the EXIT trap acquire_assign_lock installed —
  # both the `die` calls and the two bare `git worktree add` statements, which
  # exit under `set -e` rather than through die(). An EXIT trap fires either way.
  rmdir "$lock" 2>/dev/null || true
  trap - EXIT
  printf '%s\n' "$dir"
}

# Normalize an arbitrary title fragment into a safe kebab slug. Truncate first,
# then trim a trailing hyphen so a mid-word cut never yields a dangling '-'.
sanitize_slug() {
  local raw="${1:-}"
  raw="$(printf '%s' "$raw" | tr '[:upper:]' '[:lower:]' | tr -cs 'a-z0-9' '-' | cut -c1-40)"
  raw="${raw#-}"
  raw="${raw%-}"
  [[ -n "$raw" ]] || raw="issue"
  printf '%s\n' "$raw"
}

# Integrate latest origin/main by MERGE (not rebase): no history rewrite, so the
# in-flight PR branch updates with a plain push — never a force-push. The merge
# commits are squashed away when the PR finally merges.
cmd_sync() {
  local issue="$1" dir
  [[ -n "$issue" ]] || die "sync: missing issue number"
  dir="$(issue_dir "$issue")"
  [[ -d "$dir" ]] || die "sync: no worktree for issue $issue"
  git -C "$dir" fetch --quiet origin main || die "sync: could not fetch origin/main"
  if git -C "$dir" merge --no-edit origin/main >&2; then
    return 0
  fi
  git -C "$dir" merge --abort >/dev/null 2>&1 || true
  echo "fleet: merge conflict in issue $issue — worktree left clean, drop to Gate 1" >&2
  exit 3
}

cmd_release() {
  local issue="$1" root dir branch
  [[ -n "$issue" ]] || die "release: missing issue number"
  root="$(repo_root)"
  dir="$(issue_dir "$issue")"
  branch=""
  if [[ -d "$dir" ]]; then
    branch="$(git -C "$dir" rev-parse --abbrev-ref HEAD 2>/dev/null || true)"
    git -C "$root" worktree remove --force "$dir" >&2 || rm -rf "$dir"
  fi
  git -C "$root" worktree prune >/dev/null 2>&1 || true
  if [[ -n "$branch" && "$branch" != "HEAD" ]]; then
    git -C "$root" branch -D "$branch" >/dev/null 2>&1 || true
  fi
}

# Release any worktree whose work is finished: PR merged/closed, or the issue
# itself closed with no open PR. Keeps the fleet from silting up.
cmd_reconcile() {
  command -v gh >/dev/null 2>&1 || die "reconcile: gh CLI required" 2
  local issue branch _path pr_state issue_state
  while IFS=$'\t' read -r issue branch _path; do
    [[ -n "$issue" ]] || continue
    pr_state="$(gh pr list --head "$branch" --state all --limit 1 \
      --json state --jq '.[0].state // ""' 2>/dev/null || true)"
    if [[ "$pr_state" == "MERGED" || "$pr_state" == "CLOSED" ]]; then
      echo "fleet: releasing issue $issue (PR $pr_state)" >&2
      cmd_release "$issue"
      continue
    fi
    if [[ -z "$pr_state" ]]; then
      issue_state="$(gh issue view "$issue" --json state --jq .state 2>/dev/null || true)"
      if [[ "$issue_state" == "CLOSED" ]]; then
        echo "fleet: releasing issue $issue (issue closed, no PR)" >&2
        cmd_release "$issue"
      fi
    fi
  done < <(list_worktrees)
  git -C "$(repo_root)" worktree prune >/dev/null 2>&1 || true
}

usage() {
  # Line range = the header comment block, ending at the "Exit codes:" line just
  # above `set -euo pipefail`. Growing the header without moving this range
  # truncates `--help` mid-sentence; test_fleet.sh pins the last line.
  sed -n '2,60p' "$0" | sed 's/^# \{0,1\}//'
  exit "${1:-1}"
}

main() {
  local cmd="${1:-}"
  shift || true
  case "$cmd" in
    list)      cmd_list ;;
    active)    cmd_active ;;
    count)     cmd_count ;;
    free)      cmd_free ;;
    path)      cmd_path "${1:-}" ;;
    assign)    cmd_assign "${1:-}" "${2:-}" ;;
    adopt)     cmd_adopt "${1:-}" "${2:-}" ;;
    sync)      cmd_sync "${1:-}" ;;
    release)   cmd_release "${1:-}" ;;
    reconcile) cmd_reconcile ;;
    -h | --help | help | "") usage 0 ;;
    *) die "unknown subcommand '$cmd' (try: help)" ;;
  esac
}

main "$@"
