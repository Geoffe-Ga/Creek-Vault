#!/usr/bin/env bash
# scripts/ralph/test_pr_status.sh
#
# Offline tests for creek-tools/scripts/pr-status.sh — the CI/review status
# reader the Ralph loop and humans both use to ask "is this PR mergeable yet".
# Everything runs against a fake `gh` on PATH; no network, no real repo.
#
# The dimension these tests exist for is NAME AGNOSTICISM.
#
# Issue #1141 renamed and split the CI jobs: `Code Quality & Testing` became
# `Tests & Type Checking`, and its static analysis moved out into new `Static
# Analysis` and `Pylint` jobs. A status reader that recognised its own CI by
# matching a job-name substring would have gone blind at that commit — it
# would report "no failures" for a run that failed, because the job it knew
# how to look for no longer exists.
#
# pr-status.sh does not do that today: it enumerates `.jobs[]` from the API
# and counts conclusions, so any set of job names works. That property is
# load-bearing but invisible, and invisible properties get refactored away.
# These tests make it explicit: the reader is driven through THREE different
# job-name vocabularies — the pre-#1141 names, the post-#1141 names, and a set
# of names no version of this repo has ever used — and must produce identical
# verdicts for all three.
#
# Run:  bash scripts/ralph/test_pr_status.sh
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
SRC="$REPO_ROOT/creek-tools/scripts/pr-status.sh"
WATCH_SRC="$REPO_ROOT/scripts/ralph/watch-pr.sh"
READY_SRC="$REPO_ROOT/scripts/ralph/pr-ready.sh"

PASS=0
FAIL=0

ok()  { PASS=$((PASS + 1)); printf '  ok  - %s\n' "$1"; }
bad() { FAIL=$((FAIL + 1)); printf 'FAIL  - %s\n' "$1"; }
check() { # check <desc> <expected> <actual>
  if [[ "$2" == "$3" ]]; then ok "$1"; else bad "$1 (expected '$2', got '$3')"; fi
}
contains() { # contains <desc> <needle> <haystack>
  if [[ "$3" == *"$2"* ]]; then ok "$1"; else bad "$1 (no '$2' in: $3)"; fi
}
lacks() { # lacks <desc> <needle> <haystack>
  if [[ "$3" != *"$2"* ]]; then ok "$1"; else bad "$1 (unexpected '$2' in: $3)"; fi
}

if [[ ! -x "$SRC" ]]; then
  echo "FAIL  - pr-status.sh not found or not executable at $SRC" >&2
  exit 1
fi

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
BIN="$WORK/bin"
mkdir -p "$BIN"

# --- the fake gh -----------------------------------------------------------
# Dispatches on the subcommand and answers from env vars the tests set:
#   JOBS_JSON     the `.jobs` array returned by `gh run view --json jobs`
#   RUN_STATUS    "completed" | "in_progress"
#   RUN_CONCL     "success" | "failure"
#   PR_COMMENTS   the `.comments` array returned by `gh pr view`
cat > "$BIN/gh" <<'FAKE_GH'
#!/usr/bin/env bash
set -euo pipefail
case "$1" in
  repo) echo "Geoffe-Ga/Creek-Vault" ;;
  pr)
    case "$2" in
      view)    printf '{"title":"a pr","headRefName":"feat/x","comments":%s}\n' "${PR_COMMENTS:-[]}" ;;
      checks)  echo "checks table" ;;
      *) echo "fake gh: unhandled pr $2" >&2; exit 1 ;;
    esac
    ;;
  run)
    case "$2" in
      list)  printf '[{"databaseId":42,"conclusion":"%s","status":"%s"}]\n' \
               "${RUN_CONCL:-success}" "${RUN_STATUS:-completed}" ;;
      view)  printf '{"jobs":%s}\n' "${JOBS_JSON:-[]}" ;;
      *) echo "fake gh: unhandled run $2" >&2; exit 1 ;;
    esac
    ;;
  *) echo "fake gh: unhandled $1" >&2; exit 1 ;;
esac
FAKE_GH
chmod +x "$BIN/gh"

run_status() { # run_status <pr>
  PATH="$BIN:$PATH" "$SRC" status "$1" 2>&1
}

lgtm_comment='[{"body":"## Verdict\n✅ LGTM"}]'

# --- the three job-name vocabularies ---------------------------------------
# Same shape, same conclusions, entirely different names. Every verdict below
# must come out identical across all three, because the names are data.

green_old='[
  {"name":"Code Quality & Testing (3.11)","conclusion":"success"},
  {"name":"Code Quality & Testing (3.12)","conclusion":"success"},
  {"name":"CrawDad Quality (3.11)","conclusion":"success"},
  {"name":"Quality Gate","conclusion":"success"}]'

green_new='[
  {"name":"Static Analysis","conclusion":"success"},
  {"name":"Pylint","conclusion":"success"},
  {"name":"Tests & Type Checking (3.11)","conclusion":"success"},
  {"name":"Quality Gate","conclusion":"success"}]'

green_alien='[
  {"name":"zzz-never-used-by-this-repo","conclusion":"success"},
  {"name":"☃ unicode job ☃","conclusion":"success"},
  {"name":"another one","conclusion":"success"},
  {"name":"and a fourth","conclusion":"success"}]'

echo "--- green run reports READY regardless of job vocabulary ---"
for pair in "pre-#1141 names:$green_old" "post-#1141 names:$green_new" "unknown names:$green_alien"; do
  label="${pair%%:*}"
  jobs="${pair#*:}"
  rc=0
  out="$(JOBS_JSON="$jobs" RUN_CONCL=success PR_COMMENTS="$lgtm_comment" run_status 1)" || rc=$?
  contains "$label → READY TO MERGE" "READY TO MERGE" "$out"
  contains "$label → counts all 4 jobs" "4/4 jobs green" "$out"
  check    "$label → exits 0" "0" "$rc"
done

echo "--- a failing job is named from the API, never from a literal ---"
# The failing job here is one that did not exist before #1141. A reader that
# knew job names a priori could not report it; this one must.
fail_new='[
  {"name":"Static Analysis","conclusion":"success"},
  {"name":"Pylint","conclusion":"failure"},
  {"name":"Tests & Type Checking (3.13)","conclusion":"failure"},
  {"name":"Quality Gate","conclusion":"failure"}]'
rc=0
out="$(JOBS_JSON="$fail_new" RUN_CONCL=failure PR_COMMENTS="$lgtm_comment" run_status 2)" || rc=$?
contains "post-#1141 failure → NOT READY" "NOT READY TO MERGE" "$out"
contains "post-#1141 failure → names Pylint" "✗ Pylint" "$out"
contains "post-#1141 failure → names the matrix leg" "✗ Tests & Type Checking (3.13)" "$out"
contains "post-#1141 failure → counts them" "1/4 jobs green, 3 failed" "$out"
check    "post-#1141 failure → exits 1" "1" "$rc"

# A job name this repo has never used must be surfaced verbatim too — that is
# the actual proof that nothing is matched against a known-names list.
fail_alien='[
  {"name":"a job from the future","conclusion":"failure"},
  {"name":"ok one","conclusion":"success"}]'
rc=0
out="$(JOBS_JSON="$fail_alien" RUN_CONCL=failure PR_COMMENTS="$lgtm_comment" run_status 3)" || rc=$?
contains "unknown failing job is echoed verbatim" "✗ a job from the future" "$out"
check    "unknown failing job → exits 1" "1" "$rc"

echo "--- review verdict gating is independent of CI job names ---"
rc=0
out="$(JOBS_JSON="$green_new" RUN_CONCL=success PR_COMMENTS='[]' run_status 4)" || rc=$?
contains "green CI + no review → NOT READY" "NOT READY TO MERGE" "$out"
contains "green CI + no review → NO REVIEW" "NO REVIEW" "$out"
check    "green CI + no review → exits 1" "1" "$rc"

rc=0
changes='[{"body":"## Verdict\n🔄 CHANGES_REQUESTED"}]'
out="$(JOBS_JSON="$green_new" RUN_CONCL=success PR_COMMENTS="$changes" run_status 5)" || rc=$?
contains "CHANGES_REQUESTED → NOT READY" "NOT READY TO MERGE" "$out"
check    "CHANGES_REQUESTED → exits 1" "1" "$rc"

echo "--- an in-progress run is not mistaken for a pass ---"
rc=0
out="$(JOBS_JSON="$green_new" RUN_STATUS=in_progress PR_COMMENTS="$lgtm_comment" run_status 6)" || rc=$?
contains "in-progress → IN PROGRESS" "IN PROGRESS" "$out"
contains "in-progress → NOT READY" "NOT READY TO MERGE" "$out"
check    "in-progress → exits 1" "1" "$rc"

echo "--- usage errors ---"
rc=0
out="$(PATH="$BIN:$PATH" "$SRC" status 2>&1)" || rc=$?
check "status without a PR number exits 2" "2" "$rc"
rc=0
out="$(PATH="$BIN:$PATH" "$SRC" bogus 2>&1)" || rc=$?
check "unknown subcommand exits 2" "2" "$rc"

echo "--- no CI job name is hardcoded in any status reader ---"
# The grep-level backstop for everything above. If a future edit reintroduces
# a job-name literal, the behavioural tests might still pass (the literal
# could be dead code or one branch of a fallback) while the loop quietly
# regains its dependence on a name the workflow is free to change.
JOB_NAME_RE='Code Quality & Testing|Tests & Type Checking|CrawDad Quality|Quality Gate|Build Distribution|Code Complexity Analysis|Integration & E2E|Static Analysis'
for script in "$SRC" "$WATCH_SRC" "$READY_SRC"; do
  name="$(basename "$script")"
  if [[ ! -f "$script" ]]; then
    bad "$name exists"
    continue
  fi
  # Strip comments first: prose explaining WHY names are not matched is fine.
  hits="$(grep -vE '^\s*#' "$script" | grep -nE "$JOB_NAME_RE" || true)"
  if [[ -z "$hits" ]]; then
    ok "$name matches no CI job name"
  else
    bad "$name hardcodes a CI job name: $hits"
  fi
done

# --- summary ---------------------------------------------------------------
echo
echo "pr-status tests: $PASS passed, $FAIL failed"
[[ "$FAIL" -eq 0 ]]
