#!/usr/bin/env bash
# scripts/ralph/test_pick_next.sh
#
# Offline tests for pick-next.sh's parallel-awareness logic — the solo-label
# guard, the same-epic guard, and the worktree / in-flight-PR exclusions added
# for the fleet loop (see scripts/ralph/FLEET.md).
#
# pick-next.sh talks to GitHub only through `gh ... --jq ...`, so we put a fake
# `gh` on PATH that emits the already-jq-extracted values a scenario needs. Each
# scenario writes three inputs into a scratch dir the stub reads:
#   issue_list.tsv   "<number>\t<labels-csv>" per candidate (post require/exclude)
#   pr_bodies        newline-joined open-PR bodies (for Closes/Fixes/Resolves)
#   labels/<N>       labels CSV for issue N (used to inspect active issues)
# and creates .ralph/worktrees/issue-<N> dirs to simulate live workers.
#
# Run:  bash scripts/ralph/test_pick_next.sh
set -euo pipefail

PICK="$(cd "$(dirname "$0")" && pwd)/pick-next.sh"
PASS=0
FAIL=0
check() { # check <desc> <expected> <actual>
  if [[ "$2" == "$3" ]]; then
    PASS=$((PASS + 1)); printf '  ok  - %s\n' "$1"
  else
    FAIL=$((FAIL + 1)); printf 'FAIL  - %s (expected [%s], got [%s])\n' "$1" "$2" "$3"
  fi
}

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

# --- a git repo so pick-next's worktree detection resolves a toplevel ---------
REPO="$WORK/repo"
git init -q -b main "$REPO"
(cd "$REPO" && git config user.email t@t.t && git config user.name t)

# --- fake gh: reads scenario inputs from $STUBDIR (exported per scenario) ------
BIN="$WORK/bin"; mkdir -p "$BIN"
cat > "$BIN/gh" <<'STUB'
#!/usr/bin/env bash
# Emit the value real gh would produce *after* applying --jq for each call
# pick-next.sh makes. Scenario data lives under $STUBDIR.
args="$*"
case "$args" in
  *"issue list"*)
    # Two modes. Default: emit the pre-jq'd TSV a scenario supplied (tests the
    # bash walk logic). Opt-in JSON mode ($STUBDIR/issue_json present): apply the
    # REAL --jq filter pick-next.sh passes to a JSON fixture, so the embedded
    # filter/sort (require/exclude + priority tiering) is exercised end-to-end.
    if [[ -f "$STUBDIR/issue_json" ]]; then
      filter=""; prev=""
      for a in "$@"; do
        [[ "$prev" == "--jq" ]] && { filter="$a"; break; }
        prev="$a"
      done
      jq -r "$filter" "$STUBDIR/issue_json"
    else
      cat "$STUBDIR/issue_list.tsv" 2>/dev/null || true
    fi ;;
  *"pr list"*)
    cat "$STUBDIR/pr_bodies" 2>/dev/null || true ;;
  *"issue view"*)
    # find the numeric arg (the issue number) and print its labels csv
    for tok in "$@"; do
      if [[ "$tok" =~ ^[0-9]+$ ]]; then
        cat "$STUBDIR/labels/$tok" 2>/dev/null || true
        break
      fi
    done ;;
  *) : ;;
esac
STUB
chmod +x "$BIN/gh"

# --- scenario harness ---------------------------------------------------------
# new_scenario resets a fresh STUBDIR + clean worktree state.
new_scenario() {
  STUBDIR="$WORK/scn.$1"; export STUBDIR
  rm -rf "$STUBDIR" "$REPO/.ralph"
  mkdir -p "$STUBDIR/labels"
  : > "$STUBDIR/issue_list.tsv"
  : > "$STUBDIR/pr_bodies"
}
candidate() { printf '%s\t%s\n' "$1" "$2" >> "$STUBDIR/issue_list.tsv"; }   # <num> <labels-csv>
set_labels() { printf '%s' "$2" > "$STUBDIR/labels/$1"; }                    # <num> <labels-csv>
pr_closes()  { printf 'Closes #%s\n' "$1" >> "$STUBDIR/pr_bodies"; }
worktree()   { mkdir -p "$REPO/.ralph/worktrees/issue-$1"; }
run_pick()   { (cd "$REPO" && PATH="$BIN:$PATH" "$PICK"); }

# JSON-mode helpers: build the fixture the stub feeds to pick-next's REAL --jq
# filter (mirrors `gh issue list --json number,labels`), so require/exclude
# filtering AND the priority-tier sort are exercised, not bypassed.
ij_add()      { # <num> <labels-csv>  — append one issue object
  local names
  names=$(jq -cn --arg s "$2" '$s | split(",") | map(select(length>0) | {name: .})')
  jq -cn --argjson n "$1" --argjson l "$names" '{number:$n, labels:$l}' \
    >> "$STUBDIR/issue_json.lines"
}
ij_finalize() { jq -s . "$STUBDIR/issue_json.lines" > "$STUBDIR/issue_json"; }

# 1) First worker (empty fleet) gets the lowest candidate.
new_scenario first
candidate 10 ""; candidate 11 ""; candidate 12 ""
check "first worker gets lowest issue" "10" "$(run_pick)"

# 2) An issue with a live worktree is excluded.
new_scenario worktree_excl
candidate 10 ""; candidate 11 ""; candidate 12 ""
worktree 10
check "worktree issue excluded" "11" "$(run_pick)"

# 3) An issue already covered by an open PR is excluded.
new_scenario inflight_excl
candidate 10 ""; candidate 11 ""
pr_closes 10
check "in-flight PR issue excluded" "11" "$(run_pick)"

# 4) A `solo` candidate is skipped while another worker is active.
new_scenario solo_skip
candidate 11 "solo"; candidate 12 ""
set_labels 10 ""            # active issue 10 (worktree) is not solo
worktree 10
check "solo candidate skipped when fleet active" "12" "$(run_pick)"

# 4b) The `solo` guard fires when solo is one of MANY labels — the exact case the
#     has_label fix was for (the old grep -qiwx only matched a lone `solo`).
new_scenario solo_multilabel
candidate 11 "bug,solo,backend"; candidate 12 "chore"
set_labels 10 "area,backend"
worktree 10
check "multi-label solo candidate skipped" "12" "$(run_pick)"

# 4c) An active issue with solo among many labels still monopolizes the fleet.
new_scenario active_solo_multilabel
candidate 11 "chore"
set_labels 10 "epic-x,solo,backend"
worktree 10
check "multi-label active solo blocks fills" "" "$(run_pick)"

# 5) An active `solo` issue monopolizes the fleet (nothing else is pickable).
new_scenario solo_monopoly
candidate 11 ""
set_labels 10 "solo"       # the active worktree issue is solo
worktree 10
check "active solo blocks all fills" "" "$(run_pick)"

# 6) Same-epic candidate is skipped; a different-epic one is picked.
new_scenario epic_guard
candidate 11 "epic-foo"; candidate 12 "epic-bar"
set_labels 10 "epic-foo"   # active issue shares epic-foo with candidate 11
worktree 10
check "same-epic candidate skipped, cross-epic picked" "12" "$(run_pick)"

# 7) `parallelizable` overrides the same-epic guard.
new_scenario epic_override
candidate 11 "epic-foo,parallelizable"
set_labels 10 "epic-foo"
worktree 10
check "parallelizable overrides same-epic guard" "11" "$(run_pick)"

# 8) RALPH_RESPECT_EPICS=0 disables the epic guard entirely.
new_scenario epic_disabled
candidate 11 "epic-foo"
set_labels 10 "epic-foo"
worktree 10
check "epic guard off => same-epic candidate allowed" "11" \
  "$(cd "$REPO" && PATH="$BIN:$PATH" RALPH_RESPECT_EPICS=0 "$PICK")"

# 9) Backlog drained => empty output.
new_scenario drained
check "empty candidate list => nothing" "" "$(run_pick)"

# 10) A repo path segment matching "issue-<digits>" above .ralph/worktrees must not be mistaken for an active issue.
new_scenario parent_path_issue_segment
REPO2="$WORK/issue-777-fixture/repo"
git init -q -b main "$REPO2"
(cd "$REPO2" && git config user.email t@t.t && git config user.name t)
candidate 10 ""; candidate 11 ""
mkdir -p "$REPO2/.ralph/worktrees/issue-10"
check "path segment matching issue-<n> above worktrees dir is ignored" "11" \
  "$(cd "$REPO2" && PATH="$BIN:$PATH" "$PICK")"

# --- priority tiering (JSON mode: exercises the real embedded --jq sort) ------

# 11) P0 preempts a lower, older issue: #99 (P0) beats #10 (P3).
new_scenario prio_p0_preempts
ij_add 10 "P3,agent-ready"; ij_add 99 "P0,agent-ready"; ij_finalize
check "P0 preempts older P3" "99" "$(run_pick)"

# 12) Full tier order P0<P1<P2<P3, and oldest-first WITHIN a tier.
new_scenario prio_full_order
ij_add 40 "P3"; ij_add 30 "P2"; ij_add 21 "P1"; ij_add 20 "P1"; ij_add 10 "P0"
ij_finalize
check "lowest tier wins (P0)" "10" "$(run_pick)"

new_scenario prio_within_tier
ij_add 22 "P1"; ij_add 20 "P1"; ij_add 21 "P1"; ij_finalize
check "oldest-first within a tier" "20" "$(run_pick)"

# 13) Unlabeled issue defaults to rank 1 (== P1): it beats a P2 but loses to P0.
new_scenario prio_default_rank
ij_add 5 "P2"; ij_add 9 ""; ij_add 3 "P0"; ij_finalize
check "unlabeled ranks as P1 (beats P2)" "3" \
  "$(run_pick)"                                   # P0 #3 first
new_scenario prio_default_beats_p2
ij_add 5 "P2"; ij_add 9 ""; ij_finalize
check "unlabeled (default P1) beats P2" "9" "$(run_pick)"

# 14) RALPH_DEFAULT_PRIORITY_RANK override: push unlabeled to the back (rank 3).
new_scenario prio_default_override
ij_add 9 ""; ij_add 5 "P2"; ij_finalize
check "default-rank override sends unlabeled behind P2" "5" \
  "$(cd "$REPO" && PATH="$BIN:$PATH" RALPH_DEFAULT_PRIORITY_RANK=3 "$PICK")"

# 15) require/exclude filtering still applies in JSON mode: agent-ready gate.
new_scenario prio_require_gate
ij_add 10 "P0"; ij_add 11 "P3,agent-ready"; ij_finalize
check "require agent-ready filters out ungated P0" "11" \
  "$(cd "$REPO" && PATH="$BIN:$PATH" RALPH_REQUIRE_LABELS=agent-ready "$PICK")"

# --- repo's native priority-* vocabulary maps onto the same tiers -------------

# 16) The exact production bug: a `priority-critical` issue (#1175-style) must
# preempt an OLDER non-critical backlog, not sit behind it by number.
new_scenario prio_critical_preempts
ij_add 100 "bug,frontend"; ij_add 101 "priority-medium"; ij_add 175 "bug,priority-critical,full-stack"
ij_finalize
check "priority-critical preempts older non-critical backlog" "175" "$(run_pick)"

# 17) Full priority-* tier order, oldest-first within a tier.
new_scenario prio_named_order
ij_add 40 "priority-low"; ij_add 30 "priority-medium"; ij_add 20 "priority-high"
ij_add 10 "priority-critical"; ij_finalize
check "priority-critical is tier 0" "10" "$(run_pick)"

# 18) The two vocabularies are interchangeable within a tier (P0 == critical):
# oldest of the two tier-0 issues wins regardless of which label spelling.
new_scenario prio_mixed_vocab
ij_add 50 "P0"; ij_add 40 "priority-critical"; ij_add 30 "P1"; ij_finalize
check "mixed P0/priority-critical share tier 0 (oldest wins)" "40" "$(run_pick)"

# 19) priority-high outranks a P2 and an unlabeled default (rank 1 beats 2).
new_scenario prio_high_beats_medium
ij_add 5 "priority-medium"; ij_add 9 "priority-high"; ij_finalize
check "priority-high (tier 1) beats priority-medium (tier 2)" "9" "$(run_pick)"

echo
echo "pick-next tests: $PASS passed, $FAIL failed"
[[ "$FAIL" -eq 0 ]]
