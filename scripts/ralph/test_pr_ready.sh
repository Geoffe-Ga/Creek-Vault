#!/usr/bin/env bash
# scripts/ralph/test_pr_ready.sh
#
# Offline tests for pr-ready.sh — the authoritative CI + review-verdict
# readiness check the orchestrator (ralph-tick.md Step 1) uses before merging a
# lane. CI state is keyed off the `gh pr checks` EXIT CODE (0=green, 8=pending,
# else=failed), never a text grep of its TAB-delimited output, and an LGTM
# verdict only counts when it is fresher than the PR's HEAD commit (stale-verdict
# guard). We put a fake, arg-aware `gh` on PATH and assert every classification.
#
# Run:  bash scripts/ralph/test_pr_ready.sh
set -euo pipefail

READY="$(cd "$(dirname "$0")" && pwd)/pr-ready.sh"
PASS=0
FAIL=0

ok()  { PASS=$((PASS + 1)); printf '  ok  - %s\n' "$1"; }
bad() { FAIL=$((FAIL + 1)); printf 'FAIL  - %s\n' "$1"; }
check() { # check <desc> <expected> <actual>
  if [[ "$2" == "$3" ]]; then ok "$1"; else bad "$1 (expected '$2', got '$3')"; fi
}

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
BIN="$WORK/bin"
mkdir -p "$BIN"

# Arg-aware fake gh. Behaviour is driven by env vars the test sets per case:
#   CHECKS_EC     — exit code `gh pr checks` should return (0 green / 8 pending / other failed)
#   MERGE_STATE   — mergeStateStatus (CLEAN / BEHIND / ...)
#   HEAD_DATE     — RFC3339 committedDate of the PR HEAD commit
#   VERDICT       — the "<createdAt>|<isLGTM>" scalar the verdict jq resolves to
#   COMMENTS_JSON — raw `--json comments` payload; when set, the stub runs the
#                   REAL jq with pr-ready.sh's own `--jq` expression against it,
#                   so the production verdict regex is genuinely exercised
#                   (otherwise a scalar stub would mask a broken regex).
# Real gh applies --jq, so — like test_fleet.sh — the stub emits the already
# extracted scalar and branches on which --json field the caller asked for.
cat > "$BIN/gh" <<'STUB'
#!/usr/bin/env bash
args="$*"
case "$args" in
  *"pr checks"*)            exit "${CHECKS_EC:-0}" ;;
  *"--json mergeStateStatus"*) printf '%s|%s\n' "${MERGE_STATE:-CLEAN}" "${HEAD_DATE:-}" ;;
  *"--json comments"*)
    if [[ -n "${COMMENTS_JSON:-}" ]]; then
      expr="" prev=""
      for a in "$@"; do [[ "$prev" == "--jq" ]] && expr="$a"; prev="$a"; done
      printf '%s' "$COMMENTS_JSON" | jq -rc "$expr"
    else
      printf '%s\n' "${VERDICT:-|false}"
    fi ;;
  *)                        echo '' ;;
esac
STUB
chmod +x "$BIN/gh"

run() { PATH="$BIN:$PATH" "$READY" "$@" 2>/dev/null; }

H="2026-07-01T10:00:00Z"          # HEAD commit time baseline
FRESH="2026-07-01T11:00:00Z"      # a verdict posted AFTER HEAD (valid)
STALE="2026-07-01T09:00:00Z"      # a verdict posted BEFORE HEAD (stale)

# --- usage: missing PR number exits 2 --------------------------------------
rc=0
PATH="$BIN:$PATH" "$READY" >/dev/null 2>&1 || rc=$?
check "missing PR number exits 2" "2" "$rc"

# --- pending: gh pr checks exit 8 is NEVER ready (the core bug) -------------
check "exit 8 → pending" "pending" \
  "$(CHECKS_EC=8 run 100)"

# --- CI failure surfaced: non-0/non-8 exit → ci-failed ---------------------
check "exit 1 → ci-failed" "ci-failed" \
  "$(CHECKS_EC=1 run 100)"
check "exit 2 → ci-failed" "ci-failed" \
  "$(CHECKS_EC=2 run 100)"

# --- ready: green + CLEAN + fresh LGTM -------------------------------------
check "green + CLEAN + fresh LGTM → ready" "ready" \
  "$(CHECKS_EC=0 MERGE_STATE=CLEAN HEAD_DATE=$H VERDICT="$FRESH|true" run 100)"

# --- behind: green + fresh LGTM but not up-to-date -------------------------
check "green + BEHIND + fresh LGTM → behind" "behind" \
  "$(CHECKS_EC=0 MERGE_STATE=BEHIND HEAD_DATE=$H VERDICT="$FRESH|true" run 100)"

# --- stale-verdict guard: an LGTM older than HEAD does NOT count ------------
check "green + CLEAN + STALE LGTM → awaiting-review" "awaiting-review" \
  "$(CHECKS_EC=0 MERGE_STATE=CLEAN HEAD_DATE=$H VERDICT="$STALE|true" run 100)"

# --- no verdict yet → awaiting-review --------------------------------------
check "green + CLEAN + no verdict → awaiting-review" "awaiting-review" \
  "$(CHECKS_EC=0 MERGE_STATE=CLEAN HEAD_DATE=$H VERDICT="|false" run 100)"

# --- fresh but non-LGTM verdict (e.g. changes requested) → awaiting-review --
check "green + CLEAN + fresh non-LGTM → awaiting-review" "awaiting-review" \
  "$(CHECKS_EC=0 MERGE_STATE=CLEAN HEAD_DATE=$H VERDICT="$FRESH|false" run 100)"

# --- REAL jq: exercise the production verdict regex against real bodies ----
# The verdict `claude-code-review.yml` posts is `## Verdict: <X>` at the END of a
# long `## Summary …` body. These cases feed raw comment JSON through pr-ready.sh's
# own `--jq`, so a regex that fails to match `## Verdict:` — or that reads "LGTM"
# from prose instead of the verdict line — is caught here (a scalar stub can't).
if command -v jq >/dev/null 2>&1; then
  cj() { printf '{"comments":[%s]}' "$1"; }   # wrap comment object(s) as a payload

  # Canonical `## Verdict: LGTM`, fresh + CLEAN → ready.
  check "real ## Verdict: LGTM (fresh) → ready" "ready" \
    "$(CHECKS_EC=0 MERGE_STATE=CLEAN HEAD_DATE=$H \
       COMMENTS_JSON="$(cj '{"createdAt":"'"$FRESH"'","body":"## Summary\ngood\n\n## Verdict: LGTM\n"}')" \
       run 100)"

  # `**Verdict:** CHANGES_REQUESTED` whose prose mentions "LGTM" must NOT count as
  # LGTM — the exact false-positive a whole-body match would cause.
  check "real CHANGES_REQUESTED w/ 'LGTM' in prose → awaiting-review" "awaiting-review" \
    "$(CHECKS_EC=0 MERGE_STATE=CLEAN HEAD_DATE=$H \
       COMMENTS_JSON="$(cj '{"createdAt":"'"$FRESH"'","body":"Not ready for LGTM yet.\n\n**Verdict:** CHANGES_REQUESTED\n"}')" \
       run 100)"

  # No verdict-bearing comment at all → awaiting-review.
  check "real no-verdict comment → awaiting-review" "awaiting-review" \
    "$(CHECKS_EC=0 MERGE_STATE=CLEAN HEAD_DATE=$H \
       COMMENTS_JSON="$(cj '{"createdAt":"'"$FRESH"'","body":"just a chat comment"}')" \
       run 100)"

  # Latest verdict wins: an LGTM posted after an earlier CHANGES_REQUESTED → ready.
  check "real latest-verdict-wins (LGTM after CR) → ready" "ready" \
    "$(CHECKS_EC=0 MERGE_STATE=CLEAN HEAD_DATE=$H \
       COMMENTS_JSON="$(cj '{"createdAt":"'"$STALE"'","body":"## Verdict: CHANGES_REQUESTED\n"},{"createdAt":"'"$FRESH"'","body":"## Verdict: LGTM\n"}')" \
       run 100)"

  # A real, fresh `## Verdict: LGTM` that predates HEAD is still stale → awaiting.
  check "real ## Verdict: LGTM but stale → awaiting-review" "awaiting-review" \
    "$(CHECKS_EC=0 MERGE_STATE=CLEAN HEAD_DATE=$H \
       COMMENTS_JSON="$(cj '{"createdAt":"'"$STALE"'","body":"## Verdict: LGTM\n"}')" \
       run 100)"
else
  echo "  skip - real-jq verdict-regex cases (jq not installed)"
fi

# --- summary ---------------------------------------------------------------
echo
echo "pr-ready tests: $PASS passed, $FAIL failed"
[[ "$FAIL" -eq 0 ]]
