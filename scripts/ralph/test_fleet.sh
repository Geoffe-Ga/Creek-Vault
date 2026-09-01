#!/usr/bin/env bash
# scripts/ralph/test_fleet.sh
#
# Offline tests for fleet.sh — the git/worktree/slot logic that never touches
# GitHub. We build a throwaway git repo (with an `origin` remote so `fetch` and
# `origin/main` resolve) and a fake `gh` on PATH for the reconcile and head-ref
# lookups, then exercise
# assign / adopt / list / count / free / path / sync / release / reconcile.
#
# Two behaviours get extra scrutiny at the bottom of this file:
#   * repo_root() must resolve the MAIN worktree even when fleet.sh is invoked
#     from inside a LINKED worktree — a lane's own directory is exactly where an
#     adopted worker runs `fleet.sh sync`, and `git rev-parse --show-toplevel`
#     answers with the linked worktree, making the whole fleet read as empty.
#   * adopt <issue> <pr> attaches a lane to a bot PR's EXISTING head branch
#     (dependabot & friends) instead of cutting a fresh issue/<N>-<slug> branch,
#     so fixes push to that PR and a second PR is never opened.
#
# Run:  bash scripts/ralph/test_fleet.sh
set -euo pipefail

FLEET="$(cd "$(dirname "$0")" && pwd)/fleet.sh"
PASS=0
FAIL=0

ok()   { PASS=$((PASS + 1)); printf '  ok  - %s\n' "$1"; }
bad()  { FAIL=$((FAIL + 1)); printf 'FAIL  - %s\n' "$1"; }
check() { # check <desc> <expected> <actual>
  if [[ "$2" == "$3" ]]; then ok "$1"; else bad "$1 (expected '$2', got '$3')"; fi
}

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

# Stub directory, shared with the fake `gh` written further down. Defined up here
# because the fake `uv` below must be on PATH before the FIRST assign (#1478).
BIN="$WORK/bin"; mkdir -p "$BIN"

# --- a knob-driven fake `uv` (#1478) ----------------------------------------
# assign/adopt provision the lane with `uv sync --all-extras`. A real sync needs
# the network and minutes of wall clock, and the CI job that runs this suite
# (.github/workflows/ralph-recap-tests.yml) never installs uv — so the suite
# stubs it and stays hermetic in both directions.
cat > "$BIN/uv" <<'STUB'
#!/usr/bin/env bash
# Fake `uv` for test_fleet.sh. fleet.sh runs it as `uv sync --all-extras` from
# inside <worktree>/creek-tools, so $PWD is the project.
#   UV_FAIL=1    exit non-zero, create NOTHING          (failure path)
#   UV_PARTIAL=1 create .venv/bin/python, THEN fail     (half-synced tree)
#   UV_EMPTY=1   exit 0 creating NOTHING                (liar; postcondition)
#   UV_SLEEP=<s> sleep, then succeed                    (lock-contention case)
#   UV_STDOUT=1  also chatter on STDOUT                 (proves fleet.sh's >&2)
# Refusing any subcommand but `sync` keeps this from being a blanket success.
[ "${1:-}" = "sync" ] || { echo "fake uv: unexpected invocation: $*" >&2; exit 64; }
echo "fake uv: $*" >&2
[ -n "${UV_STDOUT:-}" ] && echo "fake uv: resolved 412 packages"
[ -n "${UV_SLEEP:-}" ] && sleep "$UV_SLEEP"
if [ "${UV_FAIL:-}" = "1" ]; then echo "fake uv: sync failed" >&2; exit 1; fi
if [ "${UV_EMPTY:-}" = "1" ]; then exit 0; fi
make_interpreter() {
  mkdir -p "$PWD/.venv/bin"
  printf '#!/bin/sh\nexec /usr/bin/env python3 "$@"\n' > "$PWD/.venv/bin/python"
  chmod +x "$PWD/.venv/bin/python"
}
# Real uv writes .venv/bin/python BEFORE it installs anything, so an interrupted
# sync leaves a tree that LOOKS provisioned but has no dependencies in it.
if [ "${UV_PARTIAL:-}" = "1" ]; then
  make_interpreter
  echo "fake uv: sync failed after creating the interpreter" >&2
  exit 1
fi
make_interpreter
STUB
chmod +x "$BIN/uv"
# Only a DEDICATED single-entry directory goes on the global PATH. Putting $BIN
# itself there would falsify the "run() has no gh at all on PATH" invariant the
# adopt section documents, and would globally expose the ls-remote-breaking
# $BIN/git shim written near the end of this file.
UVBIN="$WORK/bin-uv"; mkdir -p "$UVBIN"
ln -s "$BIN/uv" "$UVBIN/uv"
export PATH="$UVBIN:$PATH"

# A PATH with every uv-carrying directory stripped, for the tooling-missing case.
# No arrays and no `cond && continue` (errexit-hostile as a loop body's last
# command), so this stays shellcheck-clean at --severity=warning.
nouv_path() {
  local out="" d
  while IFS= read -r d; do
    if [[ -z "$d" || -x "$d/uv" ]]; then continue; fi
    out="${out:+$out:}$d"
  done < <(printf '%s\n' "${PATH//:/$'\n'}")
  printf '%s\n' "$out"
}
NOUV_PATH="$(nouv_path)"

# --- build an upstream + working clone -------------------------------------
git init -q -b main "$WORK/upstream"
(
  cd "$WORK/upstream"
  git config user.email t@t.t && git config user.name t
  mkdir -p scripts/ralph
  printf '{"max_workers": 4, "parallel_enabled": true}\n' > scripts/ralph/state.json
  # A lane is a checkout of the creek-tools/ project, and provisioning acts on it
  # (#1478). Without this the fixture has nothing for `uv sync` to sync, and the
  # provisioning cases would fail on a missing project rather than on the defect.
  mkdir -p creek-tools
  printf '[project]\nname = "creek-tools-fixture"\nversion = "0.0.0"\n' > creek-tools/pyproject.toml
  # Mirrors the real repo's .gitignore (`**/.venv/`). Load-bearing, not decoration:
  # the sync-conflict case and the #1180 round trip below both run `git add -A`
  # INSIDE a lane (the latter also pushes), and would otherwise commit a
  # provisioned venv into the fixture upstream.
  printf '.venv/\n**/.venv/\n' > .gitignore
  git add -A && git commit -qm init
)
git clone -q "$WORK/upstream" "$WORK/repo"
REPO="$WORK/repo"
(cd "$REPO" && git config user.email t@t.t && git config user.name t)

run() { (cd "$REPO" && "$FLEET" "$@"); }

# --- empty fleet ------------------------------------------------------------
check "count starts at 0" "0" "$(run count)"
check "free starts at 4"  "4" "$(run free)"
check "active empty"      ""  "$(run active)"

# --- assign creates a worktree + branch ------------------------------------
DIR="$(run assign 101 'Add Widget Endpoint!!' 2>/dev/null)"
[[ -d "$DIR" ]] && ok "assign created worktree dir" || bad "assign created worktree dir"
check "count is 1 after assign"    "1"   "$(run count)"
check "free drops to 3"            "3"   "$(run free)"
check "active lists issue"         "101" "$(run active)"
BR="$(cd "$DIR" && git rev-parse --abbrev-ref HEAD)"
check "branch slug sanitized"      "issue/101-add-widget-endpoint" "$BR"
check "path resolves"              "$DIR" "$(run path 101)"

# --- assign is idempotent (re-entrant) -------------------------------------
DIR2="$(run assign 101 'whatever' 2>/dev/null)"
check "re-assign returns same dir" "$DIR" "$DIR2"
check "count still 1"              "1"   "$(run count)"

# --- second worker ---------------------------------------------------------
run assign 102 'frontend tweak' >/dev/null 2>&1
check "count is 2"                 "2"   "$(run count)"
check "active lists both"          "101 102" "$(run active)"

# --- cap enforcement (parallel_enabled=false ⇒ effective cap 1) ------------
printf '{"max_workers": 4, "parallel_enabled": false}\n' > "$REPO/scripts/ralph/state.json"
check "free is 0 when sequential + active" "0" "$(run free)"
if run assign 103 'blocked by cap' >/dev/null 2>&1; then
  bad "assign refused when fleet full"
else
  ok "assign refused when fleet full"
fi
# restore parallel config
printf '{"max_workers": 4, "parallel_enabled": true}\n' > "$REPO/scripts/ralph/state.json"

# --- sync clean: merge advanced main into the branch -----------------------
(
  cd "$WORK/upstream"
  echo hello > NEWFILE.txt && git add -A && git commit -qm "advance main"
)
if run sync 101 >/dev/null 2>&1; then ok "clean sync exits 0"; else bad "clean sync exits 0"; fi
[[ -f "$DIR/NEWFILE.txt" ]] && ok "synced worktree has new main file" \
  || bad "synced worktree has new main file"

# --- sync conflict exits 3 and leaves worktree clean -----------------------
(cd "$DIR" && echo "worktree side" > CONFLICT.txt && git add -A && git commit -qm "wt change")
(
  cd "$WORK/upstream"
  echo "main side" > CONFLICT.txt && git add -A && git commit -qm "main conflict"
)
rc=0
run sync 101 >/dev/null 2>&1 || rc=$?
check "conflicting sync exits 3" "3" "$rc"
if (cd "$DIR" && git status --porcelain=v1 2>/dev/null | grep -qE '^(UU|AA|DD)'); then
  bad "worktree left mid-merge"
else
  ok "worktree left clean after aborted merge"
fi

# --- release removes worktree + branch -------------------------------------
run release 101 >/dev/null 2>&1
[[ -d "$DIR" ]] && bad "release removed worktree dir" || ok "release removed worktree dir"
check "count back to 1 after release" "1" "$(run count)"
if (cd "$REPO" && git show-ref --verify --quiet refs/heads/issue/101-add-widget-endpoint); then
  bad "release deleted branch"
else
  ok "release deleted branch"
fi

# --- reconcile releases only the MERGED worktree, keeps the open one -------
# Branch-aware fake gh: only $MERGED_BRANCH reports a MERGED PR, and only issue
# $CLOSED_ISSUE reports CLOSED — so an open second worktree must survive. This
# guards against an over-broad "MERGED for everything" stub silently releasing
# healthy workers.
run assign 105 'keep me open' >/dev/null 2>&1
check "two workers before reconcile" "2" "$(run count)"
cat > "$BIN/gh" <<'STUB'
#!/usr/bin/env bash
# real gh applies --jq, so emit the already-extracted scalar — branch-aware.
#
# Knobs (env, all optional):
#   MERGED_BRANCH  the one branch whose PR reports MERGED (reconcile tests).
#   CLOSED_ISSUE   the one issue number that reports CLOSED (reconcile tests).
#   HEAD_REF       headRefName for `pr view` — emitted as "<ref>|<isFork>".
#   FORK_PR        the one PR number that reports isCrossRepository=true.
#   HEAD_LINE_RAW  when set, `pr view` emits it VERBATIM instead of building the
#                  line — used to feed adopt malformed answers ("no-separator",
#                  "trailing-|") that must fail closed.
args="$*"
case "$args" in
  *"pr view"*"--json headRefName"*)
    if [[ -n "${HEAD_LINE_RAW:-}" ]]; then printf '%s\n' "$HEAD_LINE_RAW"; exit 0; fi
    pr=""
    for tok in "$@"; do
      if [[ "$tok" =~ ^[0-9]+$ ]]; then pr="$tok"; break; fi
    done
    if [[ -n "${FORK_PR:-}" && "$pr" == "$FORK_PR" ]]; then
      printf '%s|true\n' "${HEAD_REF:-}"
    else
      printf '%s|false\n' "${HEAD_REF:-}"
    fi ;;
  *"pr list"*"--json state"*)
    if [[ "$args" == *"--head $MERGED_BRANCH"* ]]; then echo 'MERGED'; else echo ''; fi ;;
  *"pr list"*) echo '' ;;
  *"issue view"*"--json state"*)
    for tok in "$@"; do
      if [[ "$tok" =~ ^[0-9]+$ ]]; then
        if [[ "$tok" == "${CLOSED_ISSUE:-}" ]]; then echo 'CLOSED'; else echo 'OPEN'; fi
        break
      fi
    done ;;
  *) echo '' ;;
esac
STUB
chmod +x "$BIN/gh"
(cd "$REPO" && PATH="$BIN:$PATH" MERGED_BRANCH="issue/102-frontend-tweak" \
  "$FLEET" reconcile >/dev/null 2>&1)
check "reconcile released only the merged worker" "1" "$(run count)"
check "the open worker survived reconcile"        "105" "$(run active)"

# --- die() honors an explicit exit code (rc==2) -----------------------------
# cmd_reconcile's very first statement is `command -v gh || die "..." 2` — run
# it with gh absent from PATH so the die call fires deterministically. Invoke
# the running bash by absolute path ($BASH) so the interpreter is still findable
# even though PATH is emptied (the /usr/bin/env shebang would otherwise need bash
# on PATH); `command -v gh` is a builtin, so it works under the empty PATH.
EMPTYBIN="$WORK/emptybin"; mkdir -p "$EMPTYBIN"
rc=0
(cd "$REPO" && PATH="$EMPTYBIN" "$BASH" "$FLEET" reconcile) >/dev/null 2>&1 || rc=$?
check "die honors explicit exit code (reconcile w/o gh => 2)" "2" "$rc"

# --- die() still defaults to exit 1 when no code is given --------------------
rc=0
run definitely-not-a-command >/dev/null 2>&1 || rc=$?
check "die defaults to exit 1" "1" "$rc"

# --- die() message excludes the exit-code arg ---------------------------------
err="$( (cd "$REPO" && PATH="$EMPTYBIN" "$BASH" "$FLEET" reconcile) 2>&1 1>/dev/null || true )"
check "die message excludes the exit-code arg" "fleet: reconcile: gh CLI required" "$err"

# --- lock hygiene for cmd_assign ---------------------------------------------
LOCKDIR="$REPO/.ralph/worktrees/.assign.lock"

# (a) a successful assign leaves no lock dir behind.
run assign 201 'lock cleanup' >/dev/null 2>&1
[[ -d "$LOCKDIR" ]] && bad "assign left lock dir behind" || ok "assign releases lock on success"

# (b) a refused assign (fleet full) also releases the lock (trap fires on die).
printf '{"max_workers": 4, "parallel_enabled": false}\n' > "$REPO/scripts/ralph/state.json"
run assign 203 'refused, lock must release' >/dev/null 2>&1 || true
[[ -d "$LOCKDIR" ]] && bad "refused assign left lock dir behind" \
  || ok "refused assign releases lock (trap fired on die)"
printf '{"max_workers": 4, "parallel_enabled": true}\n' > "$REPO/scripts/ralph/state.json"

# (c) a pre-existing stale lock makes assign fail fast, not hang or bypass it.
mkdir -p "$LOCKDIR"
rc=0
(cd "$REPO" && FLEET_LOCK_TIMEOUT=1 "$FLEET" assign 202 'blocked by stale lock') >/dev/null 2>&1 || rc=$?
[[ "$rc" -ne 0 ]] && ok "stale lock makes assign fail fast" || bad "stale lock did not block assign"
rmdir "$LOCKDIR" 2>/dev/null || true

# =============================================================================
# From here down the live fleet is exactly lanes 105 and 201 (count 2, free 2).
# Every expectation below is written against that baseline, so new sections must
# be APPENDED here — inserting above would shift the counts the older
# assertions pin. New issue numbers avoid 101/102/103/105/201/202/203.
# =============================================================================

# --- repo_root() must resolve the MAIN worktree, not the linked one ----------
# `git rev-parse --show-toplevel` answers with whatever worktree you are STANDING
# IN, so run from a lane's own directory the fleet read as EMPTY: `list` printed
# nothing, `free` therefore reported the full cap and the orchestrator would start
# workers past max_workers (observed live: two lanes active, `free` said 4), and
# `sync` died with "no worktree for issue N". The first `worktree ` line of
# `git worktree list --porcelain` is always the MAIN worktree — that is the fix.
LANE201="$(run path 201)"
runwt() { (cd "$LANE201" && "$FLEET" "$@"); }
check "count from inside a linked worktree"  "2"       "$(runwt count)"
check "free from inside a linked worktree"   "2"       "$(runwt free)"
check "active from inside a linked worktree" "105 201" "$(runwt active)"
check "list from inside a linked worktree"   "2"       "$(runwt list | grep -c . || true)"
# path must resolve to the MAIN repo's slot, not $LANE201/.ralph/worktrees/...
check "path from inside a linked worktree"   "$LANE201" "$(runwt path 201 || true)"

# A worktree that is NOT a lane (outside .ralph/worktrees) is the other caller
# shape: the fleet must still be visible from it, and it must never be COUNTED
# as a worker — a false lane would silently eat a slot from the cap.
git -C "$REPO" worktree add -b plain-linked-wt "$WORK/plainwt" origin/main >/dev/null 2>&1 || true
check "count from a NON-lane linked worktree" "2" "$( (cd "$WORK/plainwt" && "$FLEET" count) )"
check "a non-lane worktree is not counted as a worker" "105 201" \
  "$( (cd "$WORK/plainwt" && "$FLEET" active) )"
git -C "$REPO" worktree remove --force "$WORK/plainwt" >/dev/null 2>&1 || true
git -C "$REPO" branch -D plain-linked-wt >/dev/null 2>&1 || true

# The real caller: an adopted worker's FIRST action is `fleet.sh sync <N>` run
# from inside its own worktree. Advance main so the sync has something to merge.
(
  cd "$WORK/upstream"
  echo "linked-wt sync" > LINKED.txt && git add -A && git commit -qm "advance main again"
)
if (cd "$LANE201" && "$FLEET" sync 201) >/dev/null 2>&1; then
  ok "sync works when invoked from inside the lane's own worktree"
else
  bad "sync works when invoked from inside the lane's own worktree"
fi
[[ -f "$LANE201/LINKED.txt" ]] && ok "sync from inside the lane merged the new main file" \
  || bad "sync from inside the lane merged the new main file"

# --- adopt: attach a lane to a bot PR's EXISTING head branch -----------------
# Created AFTER the clone so adopt has to fetch the ref like the real loop does.
# The slashes are deliberate: ref handling must survive them end to end.
BOT_BRANCH="dependabot/pip/creek-tools/pip-minor-and-patch-f1456b4b2b"
(
  cd "$WORK/upstream"
  git checkout -q -b "$BOT_BRANCH" main
  echo "bump" > BUMP.txt && git add -A && git commit -qm "bump pip deps"
  git checkout -q main
)
# run() has no gh at all on PATH, so an adopt through it would die at the head
# lookup instead of exercising the code under test.
run_gh() { (cd "$REPO" && PATH="$BIN:$PATH" "$FLEET" "$@"); }
export HEAD_REF="$BOT_BRANCH"
export FORK_PR=""

# Guarded so a failing adopt cannot abort the rest of the run.
ADIR="$(run_gh adopt 401 901 2>/dev/null || true)"
ADIR="${ADIR:-$WORK/adopt-missing}"
[[ -d "$ADIR" ]] && ok "adopt created a worktree dir" || bad "adopt created a worktree dir"
# Same slot naming as assign — cmd_path/list/release all key off `issue-<N>`.
check "adopt uses the standard slot name" "issue-401" "$(basename "$ADIR")"
# The branch name must be the head ref EXACTLY: cmd_reconcile finds lanes with
# `gh pr list --head "$branch"`, and the worker pushes here to update the PR.
check "adopted lane sits on the PR head branch, name intact" "$BOT_BRANCH" \
  "$( (cd "$ADIR" 2>/dev/null && git rev-parse --abbrev-ref HEAD) 2>/dev/null || true)"
# Content proves it attached to the bot's ref, not to a fresh branch off main.
[[ -f "$ADIR/BUMP.txt" ]] && ok "adopted lane has the bot branch's content" \
  || bad "adopted lane has the bot branch's content"
# A stray issue/401-* branch would mean a second PR gets opened on push.
check "adopt created no issue/401-* branch" "" \
  "$( (cd "$REPO" && git for-each-ref --format='%(refname)' 'refs/heads/issue/401-*') )"

# An adopted lane is a first-class fleet member or the cap arithmetic lies.
check "adopted lane counts toward the fleet" "3" "$(run count)"
check "adopted lane consumes a free slot"    "1" "$(run free)"
check "adopted lane is listed as active"     "105 201 401" "$(run active)"
check "path resolves for the adopted lane"   "$ADIR" "$(run path 401 || true)"

# Re-entrancy: a re-adopted tick must reuse the lane, never stack worktrees.
ADIR2="$(run_gh adopt 401 901 2>/dev/null || true)"
check "re-adopt returns the same dir"     "$ADIR" "$ADIR2"
check "re-adopt added no second worktree" "3"     "$(run count)"

# Cap enforcement: adopt is still a worker start, so it obeys max_workers.
printf '{"max_workers": 4, "parallel_enabled": false}\n' > "$REPO/scripts/ralph/state.json"
if run_gh adopt 402 902 >/dev/null 2>&1; then
  bad "adopt refused when the fleet is full"
else
  ok "adopt refused when the fleet is full"
fi
check "refused adopt created no lane" "3" "$(run count)"
# A lock leaked by the refusal path would wedge every later assign, not just this one.
[[ -d "$LOCKDIR" ]] && bad "refused adopt left the assign lock behind" \
  || ok "refused adopt leaves no assign lock behind"
printf '{"max_workers": 4, "parallel_enabled": true}\n' > "$REPO/scripts/ralph/state.json"

# A fork PR's head branch does not exist in this repo; adopting one either fails
# obscurely or attaches to a same-named base-repo branch and pushes to it.
rc=0
( export FORK_PR=903; run_gh adopt 403 903 ) >/dev/null 2>&1 || rc=$?
[[ "$rc" -ne 0 ]] && ok "adopt refuses a cross-repository (fork) PR" \
  || bad "adopt refuses a cross-repository (fork) PR"
if run path 403 >/dev/null 2>&1; then
  bad "refused fork adopt left a worktree behind"
else
  ok "refused fork adopt left no worktree behind"
fi

# Non-numeric args reach `issue_dir`/`gh` unchecked otherwise — an issue of "abc"
# creates a slot no other subcommand can ever address.
rc=0
run_gh adopt abc 901 >/dev/null 2>&1 || rc=$?
[[ "$rc" -ne 0 ]] && ok "adopt refuses a non-numeric issue" || bad "adopt refuses a non-numeric issue"
rc=0
run_gh adopt 404 xyz >/dev/null 2>&1 || rc=$?
[[ "$rc" -ne 0 ]] && ok "adopt refuses a non-numeric PR" || bad "adopt refuses a non-numeric PR"
if run path 404 >/dev/null 2>&1; then
  bad "adopt with a bad PR arg left a worktree behind"
else
  ok "adopt with a bad PR arg left no worktree behind"
fi

# An empty headRefName (`|false`) is what gh's `// ""` default emits when the PR
# does not exist — adopting "" would attach the lane to whatever HEAD happens to be.
rc=0
( export HEAD_REF=""; run_gh adopt 406 906 ) >/dev/null 2>&1 || rc=$?
[[ "$rc" -ne 0 ]] && ok "adopt refuses an empty headRefName" || bad "adopt refuses an empty headRefName"

# Divergence guard: a local branch of the same name that is NOT the remote's is
# somebody's unpushed work. Attaching (or resetting) to origin/<ref> would throw
# it away silently. Build a true divergence — each side has a commit the other lacks.
DIV_BRANCH="dependabot/pip/creek-tools/diverged"
(
  cd "$WORK/upstream"
  git checkout -q -b "$DIV_BRANCH" main
  echo "remote side" > DIVERGE.txt && git add -A && git commit -qm "bot pushed a newer bump"
  git checkout -q -b other-work main
  echo "local side" > LOCALWORK.txt && git add -A && git commit -qm "unpushed local work"
  git checkout -q main
)
(
  cd "$REPO"
  git fetch -q origin "+refs/heads/$DIV_BRANCH:refs/remotes/origin/$DIV_BRANCH"
  git fetch -q origin "+refs/heads/other-work:refs/remotes/origin/other-work"
  git branch "$DIV_BRANCH" refs/remotes/origin/other-work
)
DIV_BEFORE="$( (cd "$REPO" && git rev-parse "refs/heads/$DIV_BRANCH") )"
rc=0
( export HEAD_REF="$DIV_BRANCH"; run_gh adopt 405 905 ) >/dev/null 2>&1 || rc=$?
[[ "$rc" -ne 0 ]] && ok "adopt refuses a local branch diverged from origin/<ref>" \
  || bad "adopt refuses a local branch diverged from origin/<ref>"
if run path 405 >/dev/null 2>&1; then
  bad "refused diverged adopt left a worktree behind"
else
  ok "refused diverged adopt left no worktree behind"
fi
check "diverged adopt left the local branch untouched (no silent reset)" \
  "$DIV_BEFORE" "$( (cd "$REPO" && git rev-parse "refs/heads/$DIV_BRANCH") )"

# sync on an adopted lane must MERGE main into the bot branch. A reset/rebase
# would drop the bot's own commit (and force-push the PR).
(
  cd "$WORK/upstream"
  echo "post-adopt" > MAINAFTERADOPT.txt && git add -A && git commit -qm "advance main post-adopt"
)
if run sync 401 >/dev/null 2>&1; then ok "sync of an adopted lane exits 0"; else bad "sync of an adopted lane exits 0"; fi
[[ -f "$ADIR/MAINAFTERADOPT.txt" ]] && ok "adopted lane picked up the new main file" \
  || bad "adopted lane picked up the new main file"
[[ -f "$ADIR/BUMP.txt" ]] && ok "sync preserved the bot's own commit (merge, not reset)" \
  || bad "sync preserved the bot's own commit (merge, not reset)"

# release must clean up locally WITHOUT touching the remote branch — deleting the
# bot's ref upstream would close the PR the loop is trying to fix.
run release 401 >/dev/null 2>&1
[[ -d "$ADIR" ]] && bad "release removed the adopted worktree" || ok "release removed the adopted worktree"
if (cd "$REPO" && git show-ref --verify --quiet "refs/heads/$BOT_BRANCH"); then
  bad "release deleted the LOCAL bot branch"
else
  ok "release deleted the LOCAL bot branch"
fi
if (cd "$WORK/upstream" && git show-ref --verify --quiet "refs/heads/$BOT_BRANCH"); then
  ok "release left the REMOTE bot branch intact"
else
  bad "release left the REMOTE bot branch intact"
fi
check "count back to 2 after releasing the adopted lane" "2" "$(run count)"

# reconcile finds lanes by branch name, so it only sees an adopted lane if the
# local branch equals headRefName exactly. Re-adopt (the remote ref survived the
# release), then merge that PR and confirm ONLY that lane is released.
RE_ADIR="$(run_gh adopt 401 901 2>/dev/null || true)"
check "adopt re-attaches after a release" "issue-401" "$(basename "${RE_ADIR:-none}")"
(cd "$REPO" && PATH="$BIN:$PATH" MERGED_BRANCH="$BOT_BRANCH" "$FLEET" reconcile >/dev/null 2>&1) || true
if run path 401 >/dev/null 2>&1; then
  bad "reconcile released the merged adopted lane"
else
  ok "reconcile released the merged adopted lane"
fi
check "reconcile left the unrelated open lanes alone" "105 201" "$(run active)"

# --- '|' in a branch name must not smuggle past the fork check ---------------
# `|` is legal in a git ref. The head lookup answers "<ref>|<isFork>", so a FORK
# whose head is `main-shim|false` answers "main-shim|false|true". Split on the
# FIRST separator and you get head_ref="main-shim", is_fork="false|true" — which
# never equals "true", so the fork check stays silent and the lane attaches to
# `main-shim`, a real, unrelated base-repo branch the worker then pushes to.
(
  cd "$WORK/upstream"
  git checkout -q -b 'main-shim' main
  echo shim > SHIM.txt && git add -A && git commit -qm "shim"
  git checkout -q -b 'main-shim|false' main
  echo pipe > PIPE.txt && git add -A && git commit -qm "pipe"
  git checkout -q -b 'stray-base-branch' main
  echo stray > STRAY.txt && git add -A && git commit -qm "stray"
  git checkout -q main
)
rc=0
( export HEAD_REF='main-shim|false' FORK_PR=911; run_gh adopt 301 911 ) >/dev/null 2>&1 || rc=$?
[[ "$rc" -ne 0 ]] && ok "adopt refuses a fork PR whose head name contains '|'" \
  || bad "adopt refuses a fork PR whose head name contains '|'"
if run path 301 >/dev/null 2>&1; then
  bad "pipe-smuggled fork adopt left a worktree behind"
else
  ok "pipe-smuggled fork adopt left no worktree behind"
fi
if (cd "$REPO" && git show-ref --verify --quiet 'refs/heads/main-shim'); then
  bad "pipe-smuggled fork adopt created a local main-shim branch"
else
  ok "pipe-smuggled fork adopt created no local main-shim branch"
fi

# The legal twin: the SAME head name from a same-repo PR must still adopt, onto
# that exact whole branch — truncating at the first '|' silently lands on main-shim.
TDIR="$( ( export HEAD_REF='main-shim|false' FORK_PR=''; run_gh adopt 302 912 ) 2>/dev/null || true)"
TDIR="${TDIR:-$WORK/adopt-missing-twin}"
check "same-repo PR keeps the whole '|' branch name" "main-shim|false" \
  "$( (cd "$TDIR" 2>/dev/null && git rev-parse --abbrev-ref HEAD) 2>/dev/null || true)"
[[ -f "$TDIR/PIPE.txt" ]] && ok "the '|' lane carries that branch's content" \
  || bad "the '|' lane carries that branch's content"
[[ -f "$TDIR/SHIM.txt" ]] && bad "the '|' lane landed on main-shim instead" \
  || ok "the '|' lane is not main-shim"
run release 302 >/dev/null 2>&1

# --- malformed head lookups must fail closed ---------------------------------
# A missing separator ("stray-base-branch") or an empty isCrossRepository
# ("stray-base-branch|") is NOT harmless to shrug off: the name adopt would fall
# back to is a REAL base-repo branch, so a lenient parse attaches a worker to it.
rc=0
( export HEAD_LINE_RAW='stray-base-branch'; run_gh adopt 303 913 ) >/dev/null 2>&1 || rc=$?
[[ "$rc" -ne 0 ]] && ok "adopt refuses a head lookup with no separator" \
  || bad "adopt refuses a head lookup with no separator"
rc=0
( export HEAD_LINE_RAW='stray-base-branch|'; run_gh adopt 303 913 ) >/dev/null 2>&1 || rc=$?
[[ "$rc" -ne 0 ]] && ok "adopt refuses a head lookup with empty isCrossRepository" \
  || bad "adopt refuses a head lookup with empty isCrossRepository"
if run path 303 >/dev/null 2>&1; then
  bad "malformed head lookup left a worktree behind"
else
  ok "malformed head lookup left no worktree behind"
fi

# --- a same-named TAG must not fool the divergence guard ---------------------
# In `git rev-parse` disambiguation refs/tags beats refs/heads, but `git worktree
# add <name>` picks the BRANCH. So vetting an unqualified rev name compares a
# different object than the one checked out: here the tag points past the branch,
# the guard "sees" a divergence that does not exist and refuses a healthy adopt.
# The guard must compare refs/heads/<b> against refs/remotes/origin/<b>.
SHADOW="dependabot/pip/creek-tools/shadowed"
(
  cd "$WORK/upstream"
  git checkout -q -b "$SHADOW" main
  echo shadow > SHADOW.txt && git add -A && git commit -qm "shadowed bump"
  git checkout -q main
  echo after > AFTERSHADOW.txt && git add -A && git commit -qm "advance main past the shadow"
)
(
  cd "$REPO"
  git fetch -q origin "+refs/heads/$SHADOW:refs/remotes/origin/$SHADOW"
  git branch "$SHADOW" "refs/remotes/origin/$SHADOW"
  git fetch -q origin main
  git tag "$SHADOW" origin/main
)
SDIR="$( ( export HEAD_REF="$SHADOW"; run_gh adopt 304 914 ) 2>/dev/null || true)"
SDIR="${SDIR:-$WORK/adopt-missing-shadow}"
[[ -d "$SDIR" ]] && ok "adopt succeeds when a same-named tag shadows the branch" \
  || bad "adopt succeeds when a same-named tag shadows the branch"
check "the adopted lane is the BRANCH tip, not the tag's object" \
  "$( (cd "$REPO" && git rev-parse "refs/heads/$SHADOW") )" \
  "$( (cd "$SDIR" 2>/dev/null && git rev-parse HEAD) 2>/dev/null || true)"
run release 304 >/dev/null 2>&1

# --- assign must REATTACH to a remote branch release deleted locally (#1180) --
# `release` ends with `git branch -D` (fleet.sh's cmd_release), so the local ref
# is gone while the PR's branch is very much alive on the remote. A later
# `assign` for the same issue then found no `refs/heads/<branch>`, took the
# else-path, and cut a BRAND NEW branch of the same name off `origin/main` —
# tracking `origin/main`, carrying none of the PR's commits, and looking
# perfectly healthy in `fleet.sh list`.
#
# OBSERVED LIVE (2026-08-06, PR #1117 re-attached after its lane was released to
# free a slot): `sync` answered "Already up to date." — of course it did, the
# lane WAS main — and `git status -sb` read `issue/1074-…...origin/main`. The
# next step in the normal flow is a push, which is either rejected
# (non-fast-forward) or, if somebody reaches for `--force`, destroys every commit
# on the PR branch. Only the loop's no-force-push rule kept this from being data
# loss.
#
# The round trip below is that incident: commit on the lane, push it, release,
# re-assign. It FAILS against the pre-fix HEAD with the lane sitting on
# `origin/main`'s tip and no PRWORK.txt anywhere.
REMOTE_BRANCH="issue/501-tracer-skeleton"
D501="$(run assign 501 'Tracer Skeleton' 2>/dev/null)"
(
  cd "$D501"
  echo "the PR's work" > PRWORK.txt
  git add -A && git commit -qm "the commit the PR is made of"
  git push -q origin "HEAD:refs/heads/$REMOTE_BRANCH"
)
PR_TIP="$( (cd "$D501" && git rev-parse HEAD) )"
run release 501 >/dev/null 2>&1
# The precondition the whole case rests on: release really did delete the local
# ref, so the re-assign genuinely has nothing local to reattach to.
if (cd "$REPO" && git show-ref --verify --quiet "refs/heads/$REMOTE_BRANCH"); then
  bad "release left the local ref behind — the #1180 round trip is not being exercised"
else
  ok "release deleted the local ref (the precondition for #1180)"
fi
if (cd "$WORK/upstream" && git show-ref --verify --quiet "refs/heads/$REMOTE_BRANCH"); then
  ok "the PR's branch survives on the remote after release"
else
  bad "the PR's branch survives on the remote after release"
fi

D501B="$(run assign 501 'Tracer Skeleton' 2>/dev/null || true)"
D501B="${D501B:-$WORK/assign-missing}"
check "re-assign lands on the PR's tip, not on origin/main" \
  "$PR_TIP" "$( (cd "$D501B" 2>/dev/null && git rev-parse HEAD) 2>/dev/null || true)"
[[ -f "$D501B/PRWORK.txt" ]] && ok "re-assigned lane carries the PR's commits" \
  || bad "re-assigned lane carries the PR's commits"
# The wrong upstream is what turns the silent wrong-state into a push failure —
# and it is the tell the incident report singled out ("## issue/1074-…...origin/main").
check "re-assign tracks origin/<branch>, not origin/main" "origin/$REMOTE_BRANCH" \
  "$( (cd "$D501B" 2>/dev/null &&
       git rev-parse --abbrev-ref --symbolic-full-name '@{upstream}') 2>/dev/null || true)"
run release 501 >/dev/null 2>&1

# The other half of the contract: a genuinely new issue has no remote branch, so
# it must still be cut from origin/main exactly as before. Without this, "prefer
# the remote" could degenerate into "never create a branch".
D502="$(run assign 502 'brand new work' 2>/dev/null || true)"
D502="${D502:-$WORK/assign-missing-new}"
check "a genuinely new issue still branches from origin/main" \
  "$( (cd "$REPO" && git rev-parse origin/main) )" \
  "$( (cd "$D502" 2>/dev/null && git rev-parse HEAD) 2>/dev/null || true)"
run release 502 >/dev/null 2>&1

# FAIL CLOSED on an unreadable remote. Creating a fresh branch off `main` when a
# remote branch MAY exist is precisely the failure above, so an lookup that
# errors (network blip, expired credential, dead remote) must abort — not fall
# through to the else-path and silently branch from main.
#
# A pass-through `git` shim that breaks ONLY `ls-remote`: the `fetch origin main`
# a few lines earlier in cmd_assign must still succeed, or the abort would prove
# nothing about the lookup. Absolute path to the real git baked in at generation
# time, because the shim shadows `git` on PATH and would otherwise re-exec itself.
REAL_GIT="$(command -v git)"
cat > "$BIN/git" <<STUB
#!/usr/bin/env bash
# fleet.sh invokes it as \`git -C <root> ls-remote …\`; the bare form is covered too.
if [[ "\${1:-}" == "ls-remote" || "\${3:-}" == "ls-remote" ]]; then
  echo "fatal: could not read from remote repository" >&2
  exit 128
fi
exec "$REAL_GIT" "\$@"
STUB
chmod +x "$BIN/git"
rc=0
(cd "$REPO" && PATH="$BIN:$PATH" "$FLEET" assign 503 'unreadable remote') >/dev/null 2>&1 || rc=$?
[[ "$rc" -ne 0 ]] && ok "assign aborts when the remote branch lookup is unreadable" \
  || bad "assign aborts when the remote branch lookup is unreadable"
if run path 503 >/dev/null 2>&1; then
  bad "an unreadable remote lookup left a worktree behind"
else
  ok "an unreadable remote lookup left no worktree behind"
fi
if (cd "$REPO" && git show-ref --verify --quiet 'refs/heads/issue/503-unreadable-remote'); then
  bad "an unreadable remote lookup branched off main anyway — the #1180 failure, fail-open"
else
  ok "an unreadable remote lookup created no branch off main"
fi
# A refused assign must still release the lock, exactly like the cap refusal above.
[[ -d "$LOCKDIR" ]] && bad "the unreadable-remote refusal left the assign lock behind" \
  || ok "the unreadable-remote refusal leaves no assign lock behind"
rm -f "$BIN/git"

# =============================================================================
# assign/adopt must hand back a RUNNABLE lane, not just a checked-out one (#1478)
# =============================================================================
# A worker's first gate is `cd creek-tools && ./scripts/check-all.sh`, and every
# script under creek-tools/scripts/ resolves a BARE `python`
# (creek-tools/scripts/_lib.sh). A lane with no .venv fails Gate 2 on missing dev
# deps — never on its own change.
#
# The section raises max_workers for its own lanes and restores the baseline at
# the end, so the two closing invariants below still see exactly 105 and 201.
printf '{"max_workers": 8, "parallel_enabled": true}\n' > "$REPO/scripts/ralph/state.json"

# fleet.sh derives every lane path from `git worktree list`, which reports the
# PHYSICAL path. On macOS `mktemp -d` hands back /var/... — a symlink to
# /private/var/... — so a path built by hand from $REPO would differ from
# fleet.sh's answer by that prefix alone. Existing cases never hit this because
# they compare fleet.sh output against fleet.sh output; the discriminators below
# are the first to construct an expected lane path independently.
REPO_P="$(cd "$REPO" && pwd -P)"

# --- assign must hand the lane a usable Python interpreter -------------------
D601="$(run assign 601 'provisioned lane' 2>/dev/null || true)"
D601="${D601:-$WORK/assign-missing-601}"

# DISCRIMINATORS. If EITHER of these fails, the case below is a harness error,
# not the defect — fix the harness before believing the RED.
check "the lane worktree was created at the expected path" \
  "$REPO_P/.ralph/worktrees/issue-601" "$D601"
check "the fake uv is on PATH for lane assigns" "$UVBIN/uv" "$(command -v uv)"
[[ -f "$D601/creek-tools/pyproject.toml" ]] \
  && ok  "the lane worktree carries creek-tools/pyproject.toml" \
  || bad "the lane worktree carries creek-tools/pyproject.toml"

# THE assertion.
[[ -x "$D601/creek-tools/.venv/bin/python" ]] \
  && ok  "assign provisions the lane venv (creek-tools/.venv/bin/python)" \
  || bad "assign provisions the lane venv (creek-tools/.venv/bin/python)"

# --- STDOUT DISCIPLINE ------------------------------------------------------
# ralph-tick.md captures BOTH `assign` and `adopt` stdout as the worktree path
# (`WT=$(scripts/ralph/fleet.sh assign …)`). One byte of provisioning chatter on
# stdout and the orchestrator dispatches a worker at a garbage path. UV_STDOUT=1
# makes the stub chatter on stdout too, so a dropped `>&2` is caught here.
run release 601 >/dev/null 2>&1
OUT601="$( (cd "$REPO" && UV_STDOUT=1 "$FLEET" assign 601 'provisioned lane') 2>/dev/null )"
check "assign prints ONLY the worktree path on stdout" \
  "$REPO_P/.ralph/worktrees/issue-601" "$OUT601"
run release 601 >/dev/null 2>&1
ERR601="$( (cd "$REPO" && "$FLEET" assign 601 'provisioned lane') 2>&1 1>/dev/null || true )"
case "$ERR601" in
  *"provisioning"*) ok  "provisioning chatter goes to stderr" ;;
  *)                bad "provisioning chatter goes to stderr" ;;
esac
run release 601 >/dev/null 2>&1

# --- SWEEP: adopt creates lanes by the same mechanism and runs the same gate --
ADIRV="$( ( export HEAD_REF="$BOT_BRANCH"; run_gh adopt 602 921 ) 2>/dev/null || true)"
ADIRV="${ADIRV:-$WORK/adopt-missing-602}"
[[ -x "$ADIRV/creek-tools/.venv/bin/python" ]] \
  && ok  "adopt provisions the lane venv too" \
  || bad "adopt provisions the lane venv too"
check "adopt prints ONLY the worktree path on stdout" \
  "$REPO_P/.ralph/worktrees/issue-602" "$ADIRV"
run release 602 >/dev/null 2>&1

# --- FAILURE PATH: refuse rather than hand back a half-built lane ------------
rc=0
OUT603="$( (cd "$REPO" && UV_FAIL=1 "$FLEET" assign 603 'failing provision') 2>/dev/null )" || rc=$?
[[ "$rc" -ne 0 ]] && ok "a failed provision makes assign exit non-zero" \
  || bad "a failed provision makes assign exit non-zero"
check "a failed provision prints nothing on stdout" "" "$OUT603"
ERR603="$( (cd "$REPO" && UV_FAIL=1 "$FLEET" assign 603 'failing provision') 2>&1 1>/dev/null || true )"
case "$ERR603" in
  *"$REPO_P/.ralph/worktrees/issue-603"*) ok  "the failure diagnostic names the worktree path" ;;
  *)                                      bad "the failure diagnostic names the worktree path" ;;
esac
case "$ERR603" in
  *"uv sync --all-extras"*) ok  "the failure diagnostic names the remediation" ;;
  *)                        bad "the failure diagnostic names the remediation" ;;
esac
# A leaked lock would wedge every later assign, not just this one.
[[ -d "$LOCKDIR" ]] && bad "a failed provision left the assign lock behind" \
  || ok "a failed provision releases the assign lock"

# --- FAILURE PATH must be NON-DESTRUCTIVE ------------------------------------
# ralph-tick.md re-assigns an EXISTING lane ("re-attach a worktree with
# `fleet.sh assign` if reconcile removed it"), and the refill loop re-invokes
# assign every pass. A lane holds a worker's uncommitted implementation, so a
# transient uv failure must never remove it. Removing it would also stop
# `fleet.sh free` decrementing, spinning the refill loop forever.
touch "$REPO/.ralph/worktrees/issue-603/WORKER_WIP.txt"
(cd "$REPO" && UV_FAIL=1 "$FLEET" assign 603 'failing provision') >/dev/null 2>&1 || true
[[ -f "$REPO/.ralph/worktrees/issue-603/WORKER_WIP.txt" ]] \
  && ok  "a failed provision leaves the lane worktree (and its uncommitted work) alone" \
  || bad "a failed provision leaves the lane worktree (and its uncommitted work) alone"

# --- A HALF-SYNCED TREE must not be left looking healthy ---------------------
# Real uv writes .venv/bin/python before it installs anything, so an interrupted
# sync leaves a tree that satisfies the readiness check while carrying none of
# the dev deps the gate needs. Left in place it would poison every later assign:
# the lane reads as provisioned forever and is never repaired, silently.
rc=0
(cd "$REPO" && UV_PARTIAL=1 "$FLEET" assign 604 'half-synced provision') >/dev/null 2>&1 || rc=$?
[[ "$rc" -ne 0 ]] && ok "a half-synced provision still exits non-zero" \
  || bad "a half-synced provision still exits non-zero"
[[ -e "$REPO/.ralph/worktrees/issue-604/creek-tools/.venv" ]] \
  && bad "a failed provision left a PARTIAL .venv behind (the readiness check now lies)" \
  || ok "a failed provision removes the partial .venv"
# ...and because it was removed, the next assign genuinely re-provisions.
D604="$(run assign 604 'half-synced provision' 2>/dev/null || true)"
D604="${D604:-$WORK/assign-missing-604}"
[[ -x "$D604/creek-tools/.venv/bin/python" ]] \
  && ok  "the next assign re-provisions a lane whose sync was interrupted" \
  || bad "the next assign re-provisions a lane whose sync was interrupted"
run release 604 >/dev/null 2>&1

# --- a `uv` that exits 0 without creating anything must NOT pass -------------
# Without a postcondition re-check, fleet.sh would trust the exit status and
# hand back an unrunnable lane while reporting success.
rc=0
(cd "$REPO" && UV_EMPTY=1 "$FLEET" assign 606 'lying provision') >/dev/null 2>&1 || rc=$?
[[ "$rc" -ne 0 ]] && ok "a uv that exits 0 creating nothing is still refused" \
  || bad "a uv that exits 0 creating nothing is still refused"
run release 606 >/dev/null 2>&1

# --- RE-ENTRANCY: a lane whose first provision failed must be REPAIRED --------
# The re-entrant early return is `[[ -d "$dir" ]]`, so without a readiness
# re-check a lane that failed to provision once is handed back unprovisioned
# forever — the defect, merely deferred by one call.
D603="$(run assign 603 'failing provision' 2>/dev/null || true)"
D603="${D603:-$WORK/assign-missing-603}"
[[ -x "$D603/creek-tools/.venv/bin/python" ]] \
  && ok  "a later assign repairs a lane whose first provision failed" \
  || bad "a later assign repairs a lane whose first provision failed"

# --- IDEMPOTENCY: a healthy lane's venv is never clobbered -------------------
# mkdir -p so the sentinel can be planted even against an unprovisioned lane —
# otherwise this setup step aborts the whole suite under `set -e` while the fix
# is still absent, and the RED transcript stops here. The assertion is unchanged
# either way: the sentinel must survive a re-assign of a HEALTHY lane.
mkdir -p "$D603/creek-tools/.venv"
touch "$D603/creek-tools/.venv/SENTINEL"
rc=0
D603B="$(run assign 603 'failing provision' 2>/dev/null)" || rc=$?
check "re-assigning a healthy lane exits 0" "0" "$rc"
check "re-assigning a healthy lane returns the same path" "$D603" "$D603B"
[[ -f "$D603/creek-tools/.venv/SENTINEL" ]] \
  && ok  "re-assigning a healthy lane does not clobber its venv" \
  || bad "re-assigning a healthy lane does not clobber its venv"
run release 603 >/dev/null 2>&1

# --- TOOLING MISSING: uv absent from PATH is exit 2, like the gh refusal -----
# Self-check first: on a runner that never had uv, nouv_path() is a no-op and
# this case would otherwise pass for a reason unrelated to the assertion.
check "NOUV_PATH really has no uv on it" "" "$( PATH="$NOUV_PATH" command -v uv || true)"
rc=0
(cd "$REPO" && PATH="$NOUV_PATH" "$BASH" "$FLEET" assign 605 'no uv') >/dev/null 2>&1 || rc=$?
check "assign without uv exits 2 (tooling missing)" "2" "$rc"
run release 605 >/dev/null 2>&1

# --- LOCK SAFETY: provisioning runs OUTSIDE the assign critical section ------
# FLEET_LOCK_TIMEOUT defaults to 10s and a cold `uv sync` runs for MINUTES, so
# provisioning inside the lock would make every concurrent lane start die on a
# false stale lock. FLEET_LOCK_TIMEOUT=2 against UV_SLEEP=5 is a STRICTER bound
# than the 10s default and proves the same thing in less wall clock.
( cd "$REPO" && UV_SLEEP=5 "$FLEET" assign 607 'slow provision' ) >"$WORK/slow.out" 2>"$WORK/slow.err" &
SLOWPID=$!
# Poll until the slow lane's worktree exists — a fixed sleep would make the case
# insensitive to the mutation, since whether the two calls overlap would depend
# on whether `git worktree add` happened to finish inside that window.
waited=0
while [[ ! -d "$REPO/.ralph/worktrees/issue-607" && "$waited" -lt 25 ]]; do
  sleep 0.2
  waited=$((waited + 1))
done
rc=0
(cd "$REPO" && FLEET_LOCK_TIMEOUT=2 "$FLEET" assign 608 'concurrent start') >/dev/null 2>&1 || rc=$?
check "a slow provision does not block a concurrent lane start" "0" "$rc"
wait "$SLOWPID" || true
run release 607 >/dev/null 2>&1
run release 608 >/dev/null 2>&1

# Restore the baseline the two closing invariants below are written against.
printf '{"max_workers": 4, "parallel_enabled": true}\n' > "$REPO/scripts/ralph/state.json"

# `usage()` prints a FIXED LINE RANGE of fleet.sh's header comment. Documenting
# the reattachment above grew that header, and a range left behind truncates
# `--help` mid-sentence — the silent kind of rot, since `usage` still exits 0.
# The "Exit codes:" line is the header's last, immediately above `set -euo`.
check "help prints the whole header (usage()'s line range still reaches the end)" \
  "Exit codes: 0 ok · 1 usage/not-found · 2 tooling missing · 3 merge conflict." \
  "$( (run --help 2>&1 || true) | grep -v '^[[:space:]]*$' | tail -n 1)"

# Nothing above may leak a lane: every adopt either released cleanly or refused.
check "fleet ends with only the two long-lived lanes" "105 201" "$(run active)"

# --- summary ----------------------------------------------------------------
echo
echo "fleet tests: $PASS passed, $FAIL failed"
[[ "$FAIL" -eq 0 ]]
