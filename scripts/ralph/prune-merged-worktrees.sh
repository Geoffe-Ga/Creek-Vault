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
# A worktree is removed only when ALL of these hold:
#   * it is not the main checkout, and the session's cwd is not inside it;
#   * it is on a branch (not detached) and GitHub has a MERGED PR for it;
#   * its local tip is the merged head or an ancestor of it — so a commit made
#     after the merge is never lost (squash merges make plain
#     `git branch -d` useless as this check);
#   * `git status --porcelain` is empty — uncommitted work is never lost.
# Anything else is kept and, if it has a merged PR, reported with the reason.
#
# Usage:
#   (hook)                         JSON on stdin, as Claude Code sends it
#   prune-merged-worktrees.sh --sweep [--dry-run]
#                                  run by hand from inside the repository
#
# Always exits 0: a cleanup hook must never fail the tool call it follows.
set -uo pipefail

dry_run=0
sweep=0
for arg in "$@"; do
  case "$arg" in
    --dry-run) dry_run=1 ;;
    --sweep)   sweep=1 ;;
    -h|--help) sed -n '2,29p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "prune-merged-worktrees: unknown argument: $arg" >&2; exit 0 ;;
  esac
done

event="PostToolUse"
session_cwd="$PWD"

# True when the command actually runs `gh pr merge` — not when it merely
# mentions it inside a quoted string, and not `gh pr merged-anything`.
runs_pr_merge() {
  local unquoted
  unquoted="$(printf '%s' "$1" | sed -E "s/'[^']*'//g; s/\"[^\"]*\"//g")"
  grep -Eq '(^|[;&|(`[:space:]])gh[[:space:]]+pr[[:space:]]+merge([[:space:];&|)]|$)' <<<"$unquoted"
}

if [[ $sweep -eq 0 ]]; then
  command -v jq >/dev/null 2>&1 || exit 0
  input="$(cat)"
  cmd="$(jq -r '.tool_input.command // empty' <<<"$input" 2>/dev/null)" || exit 0
  [[ -n "$cmd" ]] && runs_pr_merge "$cmd" || exit 0
  event="$(jq -r '.hook_event_name // "PostToolUse"' <<<"$input" 2>/dev/null)"
  hook_cwd="$(jq -r '.cwd // empty' <<<"$input" 2>/dev/null)"
  session_cwd="${hook_cwd:-${CLAUDE_PROJECT_DIR:-$PWD}}"
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

# Is the local tip of <branch> contained in a merged PR head for it?
# Prints nothing and returns 1 when gh fails, so a broken gh prunes nothing.
merged_status() {
  local br="$1" tip="$2" oids oid
  oids="$(cd "$main_wt" && gh pr list --head "$br" --state merged --limit 20 \
    --json headRefOid --jq '.[].headRefOid' 2>/dev/null)" || return 2
  [[ -n "$oids" ]] || return 1
  for oid in $oids; do
    git -C "$main_wt" cat-file -e "$oid^{commit}" 2>/dev/null \
      || git -C "$main_wt" fetch -q origin "$oid" 2>/dev/null || continue
    git -C "$main_wt" merge-base --is-ancestor "$tip" "$oid" 2>/dev/null && return 0
  done
  return 3
}

pruned=()
kept=()

judge() {
  local path="$1" ref="$2" br real tip rc
  [[ -n "$path" && -n "$ref" ]] || return 0
  br="${ref#refs/heads/}"
  real="$(physical "$path")"
  [[ "$real" == "$main_wt" ]] && return 0
  [[ -d "$path" ]] || return 0
  tip="$(git -C "$path" rev-parse HEAD 2>/dev/null)" || return 0

  merged_status "$br" "$tip"; rc=$?
  [[ $rc -eq 0 ]] || { [[ $rc -eq 3 ]] && kept+=("$path ($br): has commits beyond the merged PR head"); return 0; }

  if [[ "$session_cwd" == "$real" || "$session_cwd" == "$real"/* ]]; then
    kept+=("$path ($br): this session is working inside it")
    return 0
  fi
  if [[ -n "$(git -C "$path" status --porcelain 2>/dev/null)" ]]; then
    kept+=("$path ($br): has uncommitted changes")
    return 0
  fi
  if [[ $dry_run -eq 1 ]]; then
    pruned+=("would prune $path ($br)")
    return 0
  fi
  if git -C "$main_wt" worktree remove "$path" 2>/dev/null; then
    git -C "$main_wt" branch -D "$br" >/dev/null 2>&1 || true
    pruned+=("pruned $path ($br)")
  else
    kept+=("$path ($br): git worktree remove refused")
  fi
}

path=""
ref=""
while IFS= read -r line; do
  case "$line" in
    "worktree "*) path="${line#worktree }"; ref="" ;;
    "branch "*)   ref="${line#branch }" ;;
    "")           judge "$path" "$ref"; path=""; ref="" ;;
  esac
done < <(git -C "$main_wt" worktree list --porcelain; echo)

[[ $dry_run -eq 1 ]] || git -C "$main_wt" worktree prune 2>/dev/null

[[ ${#pruned[@]} -gt 0 || ${#kept[@]} -gt 0 ]] || exit 0

report="Merged-PR worktree cleanup:"
for l in ${pruned[@]+"${pruned[@]}"}; do report+=$'\n'"  $l"; done
for l in ${kept[@]+"${kept[@]}"}; do report+=$'\n'"  kept $l"; done

if [[ $sweep -eq 1 ]]; then
  printf '%s\n' "$report"
else
  jq -n --arg e "$event" --arg c "$report" \
    '{hookSpecificOutput: {hookEventName: $e, additionalContext: $c}}'
fi
exit 0
