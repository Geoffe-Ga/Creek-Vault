#!/usr/bin/env bash
# scripts/ralph/test_fleet.sh
#
# Offline tests for fleet.sh — the git/worktree/slot logic that never touches
# GitHub. We build a throwaway git repo (with an `origin` remote so `fetch` and
# `origin/main` resolve) and a fake `gh` on PATH for the reconcile test, then
# exercise assign / list / count / free / path / sync / release / reconcile.
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

# --- build an upstream + working clone -------------------------------------
git init -q -b main "$WORK/upstream"
(
  cd "$WORK/upstream"
  git config user.email t@t.t && git config user.name t
  mkdir -p scripts/ralph
  printf '{"max_workers": 4, "parallel_enabled": true}\n' > scripts/ralph/state.json
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
BIN="$WORK/bin"; mkdir -p "$BIN"
cat > "$BIN/gh" <<'STUB'
#!/usr/bin/env bash
# real gh applies --jq, so emit the already-extracted scalar — branch-aware.
args="$*"
case "$args" in
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

# --- summary ----------------------------------------------------------------
echo
echo "fleet tests: $PASS passed, $FAIL failed"
[[ "$FAIL" -eq 0 ]]
