#!/usr/bin/env bash
# scripts/ralph/test_prune_merged_worktrees.sh
#
# Suite for scripts/ralph/prune-merged-worktrees.sh, the Claude Code
# PostToolUse/PostToolUseFailure hook that removes a worktree once its PR has
# merged. Every case builds a real throwaway repo with real worktrees and puts
# a stub `gh` first on PATH that answers "which PRs merged for this branch?"
# from a fixture file — so the assertions are on what git actually has left on
# disk, not on what the script printed.
#
# Run:  bash scripts/ralph/test_prune_merged_worktrees.sh
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$DIR/../.." && pwd)"
SCRIPT="$DIR/prune-merged-worktrees.sh"
PASS=0
FAIL=0

ok()  { PASS=$((PASS + 1)); printf '  ok  - %s\n' "$1"; }
bad() { FAIL=$((FAIL + 1)); printf 'FAIL  - %s\n' "$1"; }

TMP="$(mktemp -d)"
BG_PIDS=()
cleanup() {
  for p in ${BG_PIDS[@]+"${BG_PIDS[@]}"}; do kill "$p" 2>/dev/null || true; done
  rm -rf "$TMP"
}
trap cleanup EXIT
TMP="$(cd "$TMP" && pwd -P)"

# --- stub gh -----------------------------------------------------------------
# `gh pr list --head <branch> --state merged ...` prints one line per merged PR
# recorded for <branch> in $GH_FIXTURE, as the real call's --jq renders it:
# "<head oid> <seconds since merge> <PR number>". The fixture holds
# "<branch> <oid> <age> <number>" per line. GH_FAIL=1 makes it behave like an
# unauthenticated / offline gh.
mkdir -p "$TMP/bin"
cat > "$TMP/bin/gh" <<'STUB'
#!/usr/bin/env bash
[[ -n "${GH_FAIL:-}" ]] && { echo "gh: not authenticated" >&2; exit 1; }
head=""
while [[ $# -gt 0 ]]; do
  if [[ "$1" == "--head" ]]; then head="$2"; shift; fi
  shift
done
[[ -f "${GH_FIXTURE:-/nonexistent}" ]] || exit 0
awk -v b="$head" '$1 == b { print $2, $3, $4 }' "$GH_FIXTURE"
STUB
chmod +x "$TMP/bin/gh"
export PATH="$TMP/bin:$PATH"

# --- fixture builders ---------------------------------------------------------
# new_repo <name>: a bare origin plus a main checkout with one commit pushed.
new_repo() {
  local name="$1"
  git init -q --bare "$TMP/$name-origin.git"
  git init -q -b main "$TMP/$name"
  git -C "$TMP/$name" remote add origin "$TMP/$name-origin.git"
  echo base > "$TMP/$name/README"
  git -C "$TMP/$name" add README
  git -C "$TMP/$name" commit -q -m base
  git -C "$TMP/$name" push -q origin main
  : > "$TMP/$name.fixture"
}

# add_wt <repo> <branch>: a worktree on a new branch with one pushed commit.
# Prints the worktree path.
add_wt() {
  local repo="$1" br="$2" path
  path="$TMP/$repo-wt-${br//\//-}"
  git -C "$TMP/$repo" worktree add -q -b "$br" "$path" main
  echo "$br" > "$path/work.txt"
  git -C "$path" add work.txt
  git -C "$path" commit -q -m "work on $br"
  git -C "$path" push -q origin "$br"
  printf '%s\n' "$path"
}

# mark_merged <repo> <branch> [age-seconds] [pr-number]: record the branch's
# current tip as a merged PR head, which is what GitHub reports for a
# squash-merged PR. The default age is a day — well past any grace period.
mark_merged() {
  printf '%s %s %s %s\n' "$2" "$(git -C "$TMP/$1" rev-parse "$2")" "${3:-86400}" "${4:-1}" \
    >> "$TMP/$1.fixture"
}

has_worktree() { git -C "$TMP/$1" worktree list --porcelain | grep -qxF "worktree $2"; }
has_branch()   { git -C "$TMP/$1" show-ref -q --verify "refs/heads/$2"; }

# hook_input <command> <cwd> [stdout]: the JSON Claude Code feeds the hook.
hook_input() {
  jq -n --arg c "$1" --arg d "$2" --arg o "${3:-}" \
    '{hook_event_name: "PostToolUse", tool_name: "Bash", cwd: $d,
      tool_input: {command: $c}, tool_response: {stdout: $o, stderr: ""}}'
}

# run_hook <repo> <command> [cwd] [stdout]: invoke the script exactly as
# Claude Code does — no arguments, hook JSON on stdin.
run_hook() {
  local repo="$1" cmd="$2" cwd="${3:-$TMP/$1}" stdout="${4:-}"
  hook_input "$cmd" "$cwd" "$stdout" | GH_FIXTURE="$TMP/$repo.fixture" bash "$SCRIPT"
}

context_of() { jq -r '.hookSpecificOutput.additionalContext' <<<"$1" 2>/dev/null || true; }

echo "prune-merged-worktrees.sh"

# 1. The core contract: a clean worktree whose PR merged is removed after a
#    `gh pr merge`, and its local branch goes with it.
new_repo r1
wt_merged="$(add_wt r1 feat/merged)"
mark_merged r1 feat/merged
out="$(run_hook r1 'gh pr merge 41 --squash --delete-branch')"
if ! has_worktree r1 "$wt_merged" && [[ ! -d "$wt_merged" ]]; then
  ok "merged + clean worktree is removed"
else
  bad "merged + clean worktree survived the hook"
fi
if ! has_branch r1 feat/merged; then ok "merged branch is deleted locally"; else bad "merged branch still exists locally"; fi
if grep -q "feat/merged" <<<"$out"; then ok "hook reports what it pruned"; else bad "hook output did not name the pruned branch: $out"; fi
if jq -e '.hookSpecificOutput.hookEventName == "PostToolUse" and (.hookSpecificOutput.additionalContext | length > 0)' <<<"$out" >/dev/null 2>&1; then
  ok "hook output is PostToolUse additionalContext JSON"
else
  bad "hook output is not valid hook JSON: $out"
fi

# 2. Every other worktree in the same sweep is judged on its own facts.
new_repo r2
wt_open="$(add_wt r2 feat/open)"              # PR not merged
wt_dirty="$(add_wt r2 feat/dirty)"            # merged, uncommitted edits
mark_merged r2 feat/dirty
echo edit >> "$wt_dirty/work.txt"
wt_ahead="$(add_wt r2 feat/ahead)"            # merged, then more local commits
mark_merged r2 feat/ahead
echo more > "$wt_ahead/more.txt"; git -C "$wt_ahead" add more.txt; git -C "$wt_ahead" commit -q -m more
wt_here="$(add_wt r2 feat/here)"              # merged, but the session is inside it
mark_merged r2 feat/here
git -C "$TMP/r2" checkout -q -b feat/main-checkout   # main checkout on a merged branch
mark_merged r2 feat/main-checkout
out="$(run_hook r2 'gh pr merge 7 --squash' "$wt_here/subdir-does-not-matter")"
report="$(context_of "$out")"

if has_worktree r2 "$wt_open"; then ok "unmerged worktree is kept"; else bad "unmerged worktree was removed"; fi
if has_worktree r2 "$wt_dirty" && grep -q edit "$wt_dirty/work.txt"; then ok "dirty merged worktree is kept with its edits"; else bad "dirty merged worktree was removed"; fi
# `git worktree remove` would refuse a dirty tree anyway; the script must say
# WHY it kept it, so the session can act on the uncommitted work.
if grep -q "feat/dirty): has uncommitted changes" <<<"$report"; then ok "dirty worktree is reported as uncommitted, not as a git refusal"; else bad "dirty worktree reason missing from report: $report"; fi
if grep -q "feat/ahead): has commits beyond the merged PR head" <<<"$report"; then ok "diverged worktree is reported with its reason"; else bad "diverged worktree reason missing from report: $report"; fi
if ! grep -q "feat/open" <<<"$report"; then ok "unmerged worktrees are not reported"; else bad "report names an unmerged worktree: $report"; fi
if has_worktree r2 "$wt_ahead" && has_branch r2 feat/ahead; then ok "worktree with commits beyond the merged head is kept"; else bad "worktree with unmerged commits was removed"; fi
if has_worktree r2 "$wt_here"; then ok "the session's own worktree is kept"; else bad "the session's own worktree was removed out from under it"; fi
if [[ -d "$TMP/r2/.git" ]] && has_branch r2 feat/main-checkout; then ok "main checkout is never touched"; else bad "main checkout or its branch was removed"; fi

# 3. Commands that are not a merge are a no-op, including ones that merely
#    mention it.
new_repo r3
wt_r3="$(add_wt r3 feat/r3)"
mark_merged r3 feat/r3
run_hook r3 'gh pr view 41' >/dev/null
run_hook r3 'echo "next: gh pr merge 41"' >/dev/null
run_hook r3 'gh pr merged-list' >/dev/null
if has_worktree r3 "$wt_r3"; then ok "non-merge commands do not prune"; else bad "a non-merge command pruned a worktree"; fi

# 4. A merge inside a compound command still counts.
run_hook r3 "cd /tmp && gh pr merge \"\$PR_NUM\" --squash --delete-branch" >/dev/null
if ! has_worktree r3 "$wt_r3"; then ok "merge inside a compound command prunes"; else bad "merge inside a compound command did not prune"; fi

# 5. --dry-run reports but changes nothing; --sweep works without stdin.
new_repo r4
wt_r4="$(add_wt r4 feat/r4)"
mark_merged r4 feat/r4
out="$(cd "$TMP/r4" && GH_FIXTURE="$TMP/r4.fixture" bash "$SCRIPT" --sweep --dry-run </dev/null)"
if has_worktree r4 "$wt_r4" && grep -q "would prune" <<<"$out"; then ok "--dry-run reports without removing"; else bad "--dry-run removed or did not report: $out"; fi
(cd "$TMP/r4" && GH_FIXTURE="$TMP/r4.fixture" bash "$SCRIPT" --sweep </dev/null >/dev/null)
if ! has_worktree r4 "$wt_r4"; then ok "--sweep prunes without hook input"; else bad "--sweep did not prune"; fi

# 6. Fail-soft: a broken gh never removes anything and never fails the hook.
new_repo r5
wt_r5="$(add_wt r5 feat/r5)"
mark_merged r5 feat/r5
rc=0
hook_input 'gh pr merge 1' "$TMP/r5" | GH_FAIL=1 GH_FIXTURE="$TMP/r5.fixture" bash "$SCRIPT" >/dev/null 2>&1 || rc=$?
if [[ $rc -eq 0 ]] && has_worktree r5 "$wt_r5"; then ok "gh failure keeps everything and exits 0"; else bad "gh failure: rc=$rc or worktree removed"; fi
rc=0
echo 'not json' | bash "$SCRIPT" >/dev/null 2>&1 || rc=$?
if [[ $rc -eq 0 ]]; then ok "malformed hook input exits 0"; else bad "malformed hook input exited $rc"; fi

# 7. Concurrent sessions. The sweep runs on ANY session's merge and covers
#    every worktree, so a sibling lane that is still alive must survive it.
new_repo r6
# 7a. A worktree another live process is sitting in is kept.
wt_busy="$(add_wt r6 feat/busy)"
mark_merged r6 feat/busy
(cd "$wt_busy" && exec sleep 300) &
BG_PIDS+=("$!")
# 7b. A worktree the fleet has locked is kept.
wt_locked="$(add_wt r6 feat/locked)"
mark_merged r6 feat/locked
git -C "$TMP/r6" worktree lock "$wt_locked"
# 7c. A sibling PR that merged moments ago (auto-merge) gets a grace period...
wt_fresh="$(add_wt r6 feat/fresh)"
mark_merged r6 feat/fresh 120 900
# 7d. ...but the PR THIS command merged is collected immediately, whether the
#     number is on the command line or only in gh's output.
wt_this="$(add_wt r6 feat/this)"
mark_merged r6 feat/this 5 41
wt_var="$(add_wt r6 feat/var)"
mark_merged r6 feat/var 5 55
sleep 1   # let the background process settle into its cwd
out="$(run_hook r6 'gh pr merge 41 --squash')"
report="$(context_of "$out")"
if has_worktree r6 "$wt_busy" && grep -q "feat/busy): a running process is using it" <<<"$report"; then
  ok "worktree with a live process inside is kept and reported"
else
  bad "worktree with a live process was removed or unreported: $report"
fi
if has_worktree r6 "$wt_locked" && grep -q "feat/locked): locked" <<<"$report"; then
  ok "locked worktree is kept and reported"
else
  bad "locked worktree was removed or unreported: $report"
fi
if has_worktree r6 "$wt_fresh" && grep -q "feat/fresh): PR #900 merged 2m ago" <<<"$report"; then
  ok "sibling PR merged inside the grace period is kept and reported"
else
  bad "freshly merged sibling was removed or unreported: $report"
fi
if ! has_worktree r6 "$wt_this"; then ok "the PR named on the command line is pruned immediately"; else bad "the just-merged PR (#41) was held by the grace period"; fi
run_hook r6 "gh pr merge \"\$PR_NUM\" --squash" "$TMP/r6" "✓ Squashed and merged pull request Geoffe-Ga/x#55 (feat: var)" >/dev/null
if ! has_worktree r6 "$wt_var"; then ok "the PR named only in gh's output is pruned immediately"; else bad "the just-merged PR (#55, from stdout) was held by the grace period"; fi
PRUNE_GRACE_MINUTES=1 run_hook r6 'gh pr merge 2' >/dev/null
if ! has_worktree r6 "$wt_fresh"; then ok "PRUNE_GRACE_MINUTES shortens the grace period"; else bad "PRUNE_GRACE_MINUTES=1 did not release a 2-minute-old merge"; fi
if has_worktree r6 "$wt_busy" && has_worktree r6 "$wt_locked"; then ok "live and locked worktrees survive a second sweep"; else bad "a second sweep removed a live or locked worktree"; fi

# 8. A merged head that is not in the local object store is not guessed at.
new_repo r7
wt_r7="$(add_wt r7 feat/r7)"
printf '%s %s %s %s\n' feat/r7 1111111111111111111111111111111111111111 86400 3 > "$TMP/r7.fixture"
run_hook r7 'gh pr merge 3' >/dev/null
if has_worktree r7 "$wt_r7"; then ok "unknown merged head keeps the worktree"; else bad "worktree removed against a merged head git does not have"; fi

# 9. --help prints the whole header, however long it grows.
help="$(bash "$SCRIPT" --help)"
if grep -q "^Usage:" <<<"$help" && grep -q "Always exits 0" <<<"$help" && ! grep -q "set -uo pipefail" <<<"$help"; then
  ok "--help prints the full header comment and nothing past it"
else
  bad "--help output is truncated or overruns the header: $help"
fi

# 10. The hook is actually wired: both Bash post-tool events invoke this script.
SETTINGS="$REPO_ROOT/.claude/settings.json"
for event in PostToolUse PostToolUseFailure; do
  if jq -e --arg e "$event" \
    '.hooks[$e][]? | select(.matcher == "Bash") | .hooks[] | select(.type == "command" and (.command | contains("scripts/ralph/prune-merged-worktrees.sh")))' \
    "$SETTINGS" >/dev/null 2>&1; then
    ok ".claude/settings.json wires ${event}[Bash] to the hook"
  else
    bad ".claude/settings.json does not wire ${event}[Bash] to scripts/ralph/prune-merged-worktrees.sh"
  fi
done

printf '\n%d passed, %d failed\n' "$PASS" "$FAIL"
[[ $FAIL -eq 0 ]]
