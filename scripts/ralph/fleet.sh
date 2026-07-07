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
#   assign <N> <slug>  Create (or reuse) a worktree for issue N off origin/main;
#                      prints its absolute path. Refuses if the fleet is full.
#   sync <N>         Integrate the latest origin/main into issue N's worktree
#                      branch by MERGE (no history rewrite ⇒ a plain push updates
#                      the PR; no force-push, ever). Exit 0 clean, exit 3 on
#                      conflict (merge aborted, worktree left clean).
#   release <N>      Remove issue N's worktree and delete its local branch.
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

# Resolve the repository root so the script works from any worktree/subdir.
repo_root() {
  git rev-parse --show-toplevel 2>/dev/null || die "not inside a git repository"
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

cmd_assign() {
  local issue="$1" slug="${2:-}" root dir branch base lock
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
    git -C "$root" worktree add "$dir" -b "$branch" "$base" >&2
  fi

  # Success: release the lock now and clear the trap so a later exit does not try
  # to rmdir an already-gone (or a newly re-acquired) lock.
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
  sed -n '2,48p' "$0" | sed 's/^# \{0,1\}//'
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
    sync)      cmd_sync "${1:-}" ;;
    release)   cmd_release "${1:-}" ;;
    reconcile) cmd_reconcile ;;
    -h | --help | help | "") usage 0 ;;
    *) die "unknown subcommand '$cmd' (try: help)" ;;
  esac
}

main "$@"
