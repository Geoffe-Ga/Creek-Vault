#!/usr/bin/env bash
# scripts/ralph/test_exec_bits.sh
#
# Regression test for the RECORDED git file mode of every directly-invoked
# shell script under scripts/ralph/. The mode matters because the orchestrator
# and its docs invoke these scripts by PATH (`scripts/ralph/watch-pr.sh <PR>`,
# ralph-tick.md Step 5) — a script committed 100644 exits 126 ("permission
# denied") on every fresh clone, exactly the platform the local hot watch was
# written for. CI runs the suites via `bash <file>`, which never consults the
# mode — so nothing else in CI can catch a dropped bit, and #1092 shipped both
# watch-pr.sh and test_watch_pr.sh as 100644 (issue #1096).
#
# The check reads `git ls-files -s` — the INDEX mode git will write on the next
# clone — never the working tree's stat(2) bits, so it cannot be fooled by a
# local `chmod +x` that was never staged.
#
# Run:  bash scripts/ralph/test_exec_bits.sh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
PASS=0
FAIL=0

ok()  { PASS=$((PASS + 1)); printf '  ok  - %s\n' "$1"; }
bad() { FAIL=$((FAIL + 1)); printf 'FAIL  - %s\n' "$1"; }
check() { # check <desc> <expected> <actual>
  if [[ "$2" == "$3" ]]; then ok "$1"; else bad "$1 (expected '$2', got '$3')"; fi
}

readonly EXEC_MODE=100755

# Every *.sh under scripts/ralph/ is directly invoked — the entry points
# (fleet.sh, pick-next.sh, pr-ready.sh, watch-pr.sh, main-health.sh) by the
# orchestrator or by each other, the test_*.sh suites by CI and by hand — so the
# rule is the whole glob, not a hand-kept list that a new script would silently
# dodge.
listing="$(git -C "$ROOT" ls-files -s -- 'scripts/ralph/*.sh')"

# Guard the guard: an empty listing (a moved directory, a bad pathspec) must
# FAIL loudly, or every assertion below would pass vacuously.
if [[ -n "$listing" ]]; then
  ok "git ls-files sees shell scripts under scripts/ralph/"
else
  bad "git ls-files returned no scripts/ralph/*.sh entries — pathspec broken?"
fi

# And pin the known entry points by name, so a rename cannot quietly drop one
# out of the glob's jurisdiction. main-health.sh (issue #1159) is one of them:
# pr-ready.sh invokes it as a SIBLING, so a lost exec bit there does not fail
# loudly — it degrades every behind lane to `main-not-green` and stalls the
# fleet quietly, which is exactly the class of failure this suite exists for.
#
# review-quota.sh (issue #1160) is the second sibling, and it needs pinning MORE
# than main-health.sh does, not less. A lost exec bit on main-health.sh fails
# LOUD: every behind lane reads `main-not-green` and the fleet visibly stalls
# within one wake. A lost exec bit on review-quota.sh fails SILENT — its polarity
# is inverted, so an unaskable helper falls through to today's `behind` → sync.
# Nothing stalls, nothing errors, no lane looks wrong, and the only symptom is a
# fresh LGTM destroyed by a sync days later that nobody would ever attribute to a
# file mode.
for name in fleet.sh pick-next.sh pr-ready.sh watch-pr.sh main-health.sh review-quota.sh; do
  if grep -q "scripts/ralph/$name\$" <<<"$listing"; then
    ok "listing includes $name"
  else
    bad "listing is missing scripts/ralph/$name"
  fi
done

while read -r mode _oid _stage path; do
  [[ -n "$mode" ]] || continue
  check "$path is committed executable ($EXEC_MODE)" "$EXEC_MODE" "$mode"
done <<<"$listing"

# --- summary ---------------------------------------------------------------
echo
echo "exec-bit tests: $PASS passed, $FAIL failed"
[[ "$FAIL" -eq 0 ]]
