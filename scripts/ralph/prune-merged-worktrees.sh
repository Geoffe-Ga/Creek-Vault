#!/usr/bin/env bash
# scripts/ralph/prune-merged-worktrees.sh
#
# Claude Code hook: once a PR merges, delete its local worktree and branch.
#
# Wired in .claude/settings.json on PostToolUse AND PostToolUseFailure for the
# Bash tool. Both events matter: `gh pr merge --delete-branch` exits non-zero
# when the PR's branch is checked out in another worktree (it cannot delete the
# local branch), even though the merge itself succeeded — precisely the case
# this hook exists for.
#
# The hook acts only when the Bash command ran `gh pr merge`. It then sweeps
# EVERY worktree of the repository, not just the one that merged, so worktrees
# whose PRs merged on GitHub (auto-merge, iteration-trigger.yml, the web UI) are
# collected the next time any session merges anything.
#
# Because the sweep fires on ANY session's merge, it must not pull a worktree
# out from under a sibling session (a parallel Ralph lane) that is still
# running in it. A worktree is removed only when ALL of these hold:
#   * it is not the main checkout, and the session's cwd is not inside it;
#   * it is on a branch (not detached) and GitHub has a MERGED PR for it;
#   * its local tip is the merged head or an ancestor of it — so a commit made
#     after the merge is never lost (squash merges make plain
#     `git branch -d` useless as this check);
#   * `git status --porcelain` is empty — uncommitted work is never lost;
#   * it is not locked (`git worktree lock`);
#   * no running process has its cwd inside it (read from /proc, else lsof;
#     when neither can be read, nothing is removed);
#   * its PR is the one this command merged, OR it merged more than
#     PRUNE_GRACE_MINUTES (default 30) ago — so a sibling lane whose PR
#     auto-merged moments ago can finish its own bookkeeping first.
# Anything else is kept and, if it has a merged PR, reported with the reason.
#
# Usage:
#   (hook)                         JSON on stdin, as Claude Code sends it
#   prune-merged-worktrees.sh --sweep [--dry-run]
#                                  run by hand from inside the repository
#
# Always exits 0: a cleanup hook must never fail the tool call it follows.
set -uo pipefail

readonly DEFAULT_GRACE_MINUTES=30

# merged_status outcomes.
readonly ST_MERGED=0        # tip is contained in a merged PR head
readonly ST_NOT_MERGED=1    # no merged PR for the branch
readonly ST_GH_FAILED=2     # gh errored: decide nothing
readonly ST_DIVERGED=3      # merged, but the tip has commits beyond it
readonly ST_HEAD_MISSING=4  # merged head is not in the local object store

usage() {
  awk 'NR == 1 { next } /^#/ { sub(/^# ?/, ""); print; next } { exit }' "$0"
}

dry_run=0
sweep=0
for arg in "$@"; do
  case "$arg" in
    --dry-run) dry_run=1 ;;
    --sweep)   sweep=1 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "prune-merged-worktrees: unknown argument: $arg" >&2; exit 0 ;;
  esac
done

grace_minutes="${PRUNE_GRACE_MINUTES:-$DEFAULT_GRACE_MINUTES}"
[[ "$grace_minutes" =~ ^[0-9]+$ ]] || grace_minutes=$DEFAULT_GRACE_MINUTES

event="PostToolUse"
session_cwd="$PWD"
this_merge=" "   # space-delimited PR numbers this command merged

# True when the command actually runs `gh pr merge` — not when it merely
# mentions it inside a quoted string, and not `gh pr merged-anything`.
runs_pr_merge() {
  local unquoted
  unquoted="$(printf '%s' "$1" | sed -E "s/'[^']*'//g; s/\"[^\"]*\"//g")"
  grep -Eq '(^|[;&|(`[:space:]])gh[[:space:]]+pr[[:space:]]+merge([[:space:];&|)]|$)' <<<"$unquoted"
}

# PR numbers this merge names: a literal argument (`gh pr merge 41`, or a
# .../pull/41 URL) and whatever gh printed ("merged pull request owner/repo#41").
merged_numbers() {
  local cmd="$1" response="$2"
  {
    grep -Eo 'gh[[:space:]]+pr[[:space:]]+merge[[:space:]]+[^[:space:];&|]+' <<<"$cmd" \
      | awk '{ print $NF }' | grep -Eo '^[0-9]+$|/pull/[0-9]+'
    grep -Eo '(#|/pull/)[0-9]+' <<<"$response"
  } | grep -Eo '[0-9]+' | sort -u | tr '\n' ' '
}

if [[ $sweep -eq 0 ]]; then
  command -v jq >/dev/null 2>&1 || exit 0
  input="$(cat)"
  cmd="$(jq -r '.tool_input.command // empty' <<<"$input" 2>/dev/null)" || exit 0
  [[ -n "$cmd" ]] && runs_pr_merge "$cmd" || exit 0
  event="$(jq -r '.hook_event_name // "PostToolUse"' <<<"$input" 2>/dev/null)"
  hook_cwd="$(jq -r '.cwd // empty' <<<"$input" 2>/dev/null)"
  session_cwd="${hook_cwd:-${CLAUDE_PROJECT_DIR:-$PWD}}"
  response="$(jq -r '[.tool_response // {} | .. | strings] | join("\n")' <<<"$input" 2>/dev/null)"
  this_merge=" $(merged_numbers "$cmd" "$response")"
fi

command -v gh >/dev/null 2>&1 || exit 0

# Resolve the nearest existing directory, physically, so /var vs /private/var
# and a cwd that has since vanished both compare correctly.
physical() {
  local p="$1"
  while [[ -n "$p" && ! -d "$p" ]]; do p="$(dirname "$p")"; done
  (cd "$p" 2>/dev/null && pwd -P)
}

session_cwd="$(physical "$session_cwd")" || exit 0
[[ -n "$session_cwd" ]] || exit 0
git -C "$session_cwd" rev-parse --git-dir >/dev/null 2>&1 || exit 0

# The main checkout is the first entry of `git worktree list`.
main_wt="$(git -C "$session_cwd" worktree list --porcelain | sed -n '1s/^worktree //p')"
[[ -n "$main_wt" ]] || exit 0
main_wt="$(physical "$main_wt")"

# Every running process's cwd, one per line. Run from / so this script's own
# processes never count as "using" a worktree. Returns 1 when processes cannot
# be inspected at all, and the caller then removes nothing.
live_cwds() {
  if [[ -r /proc/self/cwd ]]; then
    local d
    # A process can exit between the glob and its readlink — this loop's own
    # subshells always do — so a failed readlink is expected, and the loop's
    # last exit status says nothing about whether /proc was readable.
    for d in /proc/[0-9]*; do readlink "$d/cwd" 2>/dev/null || true; done
    return 0
  elif command -v lsof >/dev/null 2>&1; then
    lsof -d cwd -Fn 2>/dev/null | sed -n 's/^n//p'
    return 0
  fi
  return 1
}
live_ok=1
live="$(cd / && live_cwds)" || live_ok=0

in_use() {
  local real="$1" c
  while IFS= read -r c; do
    [[ -n "$c" ]] || continue
    [[ "$c" == "$real" || "$c" == "$real"/* ]] && return 0
  done <<<"$live"
  return 1
}

# Look up the merged PR containing <tip> for <branch>; on ST_MERGED sets
# MERGED_AGE (seconds since merge) and MERGED_NUMBER.
MERGED_AGE=0
MERGED_NUMBER=""
merged_status() {
  local br="$1" tip="$2" rows oid age number status=$ST_NOT_MERGED
  rows="$(cd "$main_wt" && gh pr list --head "$br" --state merged --limit 20 \
    --json headRefOid,mergedAt,number \
    --jq '.[] | "\(.headRefOid) \((now - (.mergedAt | fromdateiso8601)) | floor) \(.number)"' \
    2>/dev/null)" || return $ST_GH_FAILED
  while read -r oid age number; do
    [[ -n "$oid" ]] || continue
    if ! git -C "$main_wt" cat-file -e "$oid^{commit}" 2>/dev/null; then
      [[ $status -eq $ST_NOT_MERGED ]] && status=$ST_HEAD_MISSING
      continue
    fi
    if git -C "$main_wt" merge-base --is-ancestor "$tip" "$oid" 2>/dev/null; then
      MERGED_AGE="$age"
      MERGED_NUMBER="$number"
      return $ST_MERGED
    fi
    status=$ST_DIVERGED
  done <<<"$rows"
  return $status
}

pruned=()
kept=()

judge() {
  local path="$1" ref="$2" locked="$3" br real tip rc label
  [[ -n "$path" && -n "$ref" ]] || return 0
  br="${ref#refs/heads/}"
  label="$path ($br)"
  real="$(physical "$path")"
  [[ "$real" == "$main_wt" ]] && return 0
  [[ -d "$path" ]] || return 0
  tip="$(git -C "$path" rev-parse HEAD 2>/dev/null)" || return 0

  merged_status "$br" "$tip"; rc=$?
  case $rc in
    "$ST_MERGED") ;;
    "$ST_DIVERGED")     kept+=("$label: has commits beyond the merged PR head"); return 0 ;;
    "$ST_HEAD_MISSING") kept+=("$label: merged PR head is not in the local repository"); return 0 ;;
    *) return 0 ;;
  esac

  if [[ "$session_cwd" == "$real" || "$session_cwd" == "$real"/* ]]; then
    kept+=("$label: this session is working inside it"); return 0
  fi
  if [[ $locked -eq 1 ]]; then
    kept+=("$label: locked (git worktree unlock to release)"); return 0
  fi
  if [[ $live_ok -eq 0 ]]; then
    kept+=("$label: cannot see running processes (no /proc or lsof)"); return 0
  fi
  if in_use "$real"; then
    kept+=("$label: a running process is using it"); return 0
  fi
  if [[ -n "$(git -C "$path" status --porcelain 2>/dev/null)" ]]; then
    kept+=("$label: has uncommitted changes"); return 0
  fi
  if [[ "$this_merge" != *" $MERGED_NUMBER "* ]] && (( MERGED_AGE < grace_minutes * 60 )); then
    kept+=("$label: PR #$MERGED_NUMBER merged $((MERGED_AGE / 60))m ago; collected after the ${grace_minutes}m grace period")
    return 0
  fi
  if [[ $dry_run -eq 1 ]]; then
    pruned+=("would prune $label"); return 0
  fi
  if git -C "$main_wt" worktree remove "$path" 2>/dev/null; then
    git -C "$main_wt" branch -D "$br" >/dev/null 2>&1 || true
    pruned+=("pruned $label")
  else
    kept+=("$label: git worktree remove refused")
  fi
}

path=""
ref=""
locked=0
while IFS= read -r line; do
  case "$line" in
    "worktree "*) path="${line#worktree }"; ref=""; locked=0 ;;
    "branch "*)   ref="${line#branch }" ;;
    locked*)      locked=1 ;;
    "")           judge "$path" "$ref" "$locked"; path=""; ref=""; locked=0 ;;
  esac
done < <(git -C "$main_wt" worktree list --porcelain; echo)

[[ $dry_run -eq 1 ]] || git -C "$main_wt" worktree prune 2>/dev/null

[[ ${#pruned[@]} -gt 0 || ${#kept[@]} -gt 0 ]] || exit 0

report="$(
  echo "Merged-PR worktree cleanup:"
  [[ ${#pruned[@]} -gt 0 ]] && printf '  %s\n' "${pruned[@]}"
  [[ ${#kept[@]} -gt 0 ]] && printf '  kept %s\n' "${kept[@]}"
)"

if [[ $sweep -eq 1 ]]; then
  printf '%s\n' "$report"
else
  jq -n --arg e "$event" --arg c "$report" \
    '{hookSpecificOutput: {hookEventName: $e, additionalContext: $c}}'
fi
exit 0
