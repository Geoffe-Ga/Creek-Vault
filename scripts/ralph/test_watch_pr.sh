#!/usr/bin/env bash
# scripts/ralph/test_watch_pr.sh
#
# Offline tests for watch-pr.sh — the per-lane hot watch a LOCAL orchestrator
# session launches as a background task per in-flight PR (ralph-tick.md Step 5).
# The watcher polls pr-ready.sh and exits — which IS the wake — the moment the
# token leaves the in-flight set {pending, awaiting-review, main-not-green}, the
# PR merges or closes (`gone`), or TIMEOUT elapses. Like test_pr_ready.sh,
# everything runs against stubs: a fake, sequence-driven `pr-ready.sh` placed
# NEXT TO a copy of the script under test (watch-pr.sh resolves its sibling by
# dirname), and a fake `gh` on PATH for the merged/closed probe.
#
# The dimensions pinned here:
#
#   idempotence     a LIVE pidfile → `already-watching`, exit 0, and pr-ready
#                   is never consulted; a STALE pidfile (dead or garbage pid)
#                   is taken over; the pidfile is removed again on exit.
#   settle          pending→ready exits on the first non-in-flight token, and
#                   every non-in-flight token (behind, ci-failed, …) counts —
#                   each is a state the orchestrator acts on.
#   gone            a MERGED or CLOSED PR ends the watch with `gone`, checked
#                   before pr-ready so a closed lane never spins in the poll.
#   timeout         TIMEOUT prints `timeout <last-token>` (or `unknown` when
#                   no poll ever classified) and exits 0 — wait outcomes are
#                   NEVER non-zero; only usage errors exit 2.
#   error tolerance a pr-ready tooling failure (exit 2, empty stdout) or a
#                   failed `gh` state lookup keeps the loop alive — transient
#                   GitHub weather must not kill the watcher or fake a wake.
#   in-flight set   FIVE tokens keep the watcher waiting, not two:
#                   `main-not-green` (issue #1159), `review-quota-exhausted`
#                   (issue #1160) and `ci-unreadable` (issue #1408) joined
#                   {pending, awaiting-review}. All three are WAIT states — the
#                   lane is held until `main`'s CI recovers, until the reviewer's
#                   rate limit window resets, or until GitHub answers a rollup
#                   query we can actually parse, and nothing the orchestrator can
#                   do shortens any of them — so a watcher that exited on one
#                   would be relaunched immediately and exit immediately again,
#                   forever. `ci-unreadable` is the sharpest of the three: it is
#                   terminal-vs-in-flight that decides whether an unreadable probe
#                   costs one poll or a fix worker dispatched at a green tree.
#
# Run:  bash scripts/ralph/test_watch_pr.sh
set -euo pipefail

SRC="$(cd "$(dirname "$0")" && pwd)/watch-pr.sh"
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
RALPH="$WORK/ralph"
PIDDIR="$WORK/pids"
STATE="$WORK/state"
mkdir -p "$BIN" "$RALPH" "$PIDDIR" "$STATE"

# The script under test, next to its stubbed sibling: watch-pr.sh finds
# pr-ready.sh by its own dirname, so a copy alongside a fake is the seam.
cp "$SRC" "$RALPH/watch-pr.sh"
chmod +x "$RALPH/watch-pr.sh"

# Sequence-driven fake pr-ready.sh. TOKENS is a comma-separated script of
# answers, one per call (the last repeats forever); the literal `ERR` plays a
# tooling failure — exit 2, NOTHING on stdout — exactly pr-ready.sh's contract.
# A counter file exposes how many polls actually happened.
cat > "$RALPH/pr-ready.sh" <<'STUB'
#!/usr/bin/env bash
set -euo pipefail
count_file="$STATE_DIR/ready-calls"
n="$(cat "$count_file" 2>/dev/null || echo 0)"
n=$((n + 1))
echo "$n" > "$count_file"
IFS=',' read -ra toks <<< "${TOKENS:-pending}"
idx=$((n - 1))
[[ "$idx" -lt "${#toks[@]}" ]] || idx=$(( ${#toks[@]} - 1 ))
tok="${toks[$idx]}"
[[ "$tok" != "ERR" ]] || exit 2
printf '%s\n' "$tok"
STUB
chmod +x "$RALPH/pr-ready.sh"

# Fake gh for the merged/closed probe. STATES scripts `pr view --json state`
# answers the same way (last repeats; default OPEN); GH_STATE_EC plays a failed
# lookup (rate limit, dead network) — non-zero, nothing on stdout.
cat > "$BIN/gh" <<'STUB'
#!/usr/bin/env bash
args="$*"
case "$args" in
  *"pr view"*"--json state"*)
    if [[ "${GH_STATE_EC:-0}" -ne 0 ]]; then exit "${GH_STATE_EC}"; fi
    count_file="$STATE_DIR/gh-state-calls"
    n="$(cat "$count_file" 2>/dev/null || echo 0)"
    n=$((n + 1))
    echo "$n" > "$count_file"
    IFS=',' read -ra sts <<< "${STATES:-OPEN}"
    idx=$((n - 1))
    [[ "$idx" -lt "${#sts[@]}" ]] || idx=$(( ${#sts[@]} - 1 ))
    printf '%s\n' "${sts[$idx]}" ;;
  *) echo '' ;;
esac
STUB
chmod +x "$BIN/gh"

# run_watch <PR> [INTERVAL] [TIMEOUT] — state dir keyed by PR number (each case
# uses a distinct PR), because run_watch executes inside a command substitution
# and a shell variable set there would never reach the parent's assertions.
# Fast polls (0.1s) keep the whole suite in seconds. cwd is $WORK (no git repo)
# so the pidfile slug exercises the no-origin fallback path too.
run_watch() {
  local sdir="$STATE/case-$1"
  mkdir -p "$sdir"
  ( cd "$WORK" &&
    PATH="$BIN:$PATH" STATE_DIR="$sdir" RALPH_WATCH_PIDDIR="$PIDDIR" \
    "$RALPH/watch-pr.sh" "$1" "${2:-0.1}" "${3:-30}" 2>/dev/null )
}
polls() { cat "$STATE/case-$1/ready-calls" 2>/dev/null || echo 0; }

# --- usage errors are the ONLY non-zero exits ------------------------------
rc=0
PATH="$BIN:$PATH" "$RALPH/watch-pr.sh" >/dev/null 2>&1 || rc=$?
check "missing PR number exits 2" "2" "$rc"

rc=0
PATH="$BIN:$PATH" "$RALPH/watch-pr.sh" abc >/dev/null 2>&1 || rc=$?
check "non-numeric PR exits 2" "2" "$rc"

rc=0
PATH="$BIN:$PATH" "$RALPH/watch-pr.sh" 100 nope >/dev/null 2>&1 || rc=$?
check "non-numeric INTERVAL exits 2" "2" "$rc"

rc=0
PATH="$BIN:$PATH" "$RALPH/watch-pr.sh" 100 1 1.5 >/dev/null 2>&1 || rc=$?
check "fractional TIMEOUT exits 2" "2" "$rc"

# --- settle: exit on the first token OUTSIDE {pending, awaiting-review} -----
rc=0
out="$(TOKENS="pending,pending,ready" run_watch 100)" || rc=$?
check "pending,pending,ready → WATCH ready" "WATCH 100 ready" "$out"
check "settle exits 0" "0" "$rc"
check "settle polled exactly 3 times" "3" "$(polls 100)"

out="$(TOKENS="awaiting-review,ci-failed" run_watch 101)"
check "awaiting-review is in-flight; ci-failed wakes" "WATCH 101 ci-failed" "$out"

# behind / optout / ready-unreviewed all leave the in-flight set immediately —
# each is orchestrator-actionable (sync, hands-off, report), so each is a wake.
out="$(TOKENS="behind" run_watch 102)"
check "behind wakes on the first poll" "WATCH 102 behind" "$out"
check "behind case polled exactly once" "1" "$(polls 102)"

out="$(TOKENS="optout" run_watch 103)"
check "optout wakes" "WATCH 103 optout" "$out"

out="$(TOKENS="ready-unreviewed" run_watch 104)"
check "ready-unreviewed wakes" "WATCH 104 ready-unreviewed" "$out"

# changes-requested — a fresh non-LGTM verdict, i.e. Gate 4 FAILED (issue
# #1097) — is deliberately NOT in IN_FLIGHT_TOKENS, so it falls out of the
# in-flight set and becomes a prompt wake with no watcher change at all. This
# pins the case observed live on PR #1095, where the old classifier called a
# fresh COMMENTS verdict `awaiting-review` and the watcher slept its full
# timeout past it.
out="$(TOKENS="awaiting-review,changes-requested" run_watch 114)"
check "changes-requested wakes promptly (outside the in-flight set)" \
  "WATCH 114 changes-requested" "$out"
check "changes-requested case polled exactly twice" "2" "$(polls 114)"

# `review-failed` (issue #1200) — the `claude-review` job itself broke while
# every other check is green, so the code needs no change and the tree is fine.
# It is ACTIONABLE (re-run that review run), which is the exact inverse of the
# #1159/#1160 argument for the four tokens that ARE in-flight: nothing resolves
# this on its own, so a watcher that slept on it would burn the full ~30-minute
# timeout and then wake having achieved nothing.
out="$(TOKENS="awaiting-review,review-failed" run_watch 120)"
check "review-failed wakes promptly (outside the in-flight set)" \
  "WATCH 120 review-failed" "$out"
check "review-failed case polled exactly twice" "2" "$(polls 120)"

# --- THE BUSY-WAKE REGRESSION TEST (issue #1159) ----------------------------
# `main-not-green` means "this lane is held until `main`'s CI goes green again",
# which is a WAIT — the orchestrator has nothing to do about it, and `main` CI
# takes ~14 minutes per round. So it belongs in IN_FLIGHT_TOKENS, and this is
# the case that proves it: the watcher must poll THROUGH it and wake only when
# the lane finally settles.
#
# If it were left OUT of that set, the failure would be worse than the bug
# #1159 fixes and much harder to see: the watcher would exit on its very first
# poll, the orchestrator would relaunch it (the pidfile is gone, so the
# idempotence guard does not catch it), it would exit immediately again, and
# the whole fleet would busy-wake at wake speed for as long as `main` stayed
# red — burning the API budget precisely when nobody can merge anything.
rc=0
out="$(TOKENS="main-not-green,main-not-green,ready" run_watch 115)" || rc=$?
check "main-not-green is in-flight; ready wakes" "WATCH 115 ready" "$out"
check "busy-wake pin exits 0" "0" "$rc"
check "busy-wake pin polled exactly 3 times (never woke early)" "3" "$(polls 115)"

# And a lane held for the whole window times out as a wait, exactly like a
# `pending` one: the last classified token rides out with it, and it is still
# exit 0 — a held lane is not an error.
rc=0
out="$(TOKENS="main-not-green" run_watch 116 0.2 1)" || rc=$?
check "main-not-green alone times out as a wait state" "WATCH 116 timeout main-not-green" "$out"
check "main-not-green timeout exits 0" "0" "$rc"

# --- THE SAME BUSY-WAKE PIN, FOR `review-quota-exhausted` (issue #1160) ------
# This token means "this lane holds a fresh LGTM and needs a sync, but the
# `claude-review` quota is exhausted, so the sync would destroy the verdict with
# no way to earn it back". Like `main-not-green` it is a WAIT: the orchestrator
# has nothing to do about it, and the remedy arrives on its own when the rate
# limit window resets.
#
# Leaving it OUT of IN_FLIGHT_TOKENS reproduces #1159's busy-wake storm — the
# watcher exits on its first poll, the orchestrator relaunches it (the pidfile is
# gone, so the idempotence guard does not catch it), it exits again — except that
# here it would run for DAYS rather than the ~20 minutes a `main` CI round takes.
# The observed window on PR #1158 was SEVEN days and had three days left to run.
# So the fleet would spin at wake speed, burning the API budget, for days,
# precisely when nobody can merge anything and the budget is the scarce thing.
rc=0
out="$(TOKENS="review-quota-exhausted,review-quota-exhausted,ready" run_watch 117)" || rc=$?
check "review-quota-exhausted is in-flight; ready wakes" "WATCH 117 ready" "$out"
check "review-quota busy-wake pin exits 0" "0" "$rc"
check "review-quota busy-wake pin polled exactly 3 times (never woke early)" "3" \
  "$(polls 117)"

# The realistic settle: the window resets, the verdict is safe to spend again,
# and pr-ready.sh goes back to reporting the lane's real state — `behind`, whose
# remedy the orchestrator can finally run.
out="$(TOKENS="review-quota-exhausted,behind" run_watch 118)"
check "the quota window resetting wakes the lane to behind" "WATCH 118 behind" "$out"
check "quota-reset case polled exactly twice" "2" "$(polls 118)"

# And a lane held for the whole window times out as a wait, exactly like a
# `pending` or `main-not-green` one: the last classified token rides out with it,
# and it is still exit 0 — a held lane is not an error.
rc=0
out="$(TOKENS="review-quota-exhausted" run_watch 119 0.2 1)" || rc=$?
check "review-quota-exhausted alone times out as a wait state" \
  "WATCH 119 timeout review-quota-exhausted" "$out"
check "review-quota-exhausted timeout exits 0" "0" "$rc"

# --- THE SAME BUSY-WAKE PIN, FOR `ci-unreadable` (issue #1408) ---------------
# This token means "pr-ready.sh could not READ the check rollup" — the probe
# errored, or answered a shape it could not reconcile with `gh pr checks` having
# exited non-zero. That is a statement about the PROBE, not about the tree, and
# like the two above it is a WAIT: the remedy is the next poll, and nothing the
# orchestrator can do makes GitHub answerable any sooner.
#
# Leaving it OUT of IN_FLIGHT_TOKENS is not merely #1159's busy-wake — IT IS
# ISSUE #1408 ITSELF, because a terminal unreadable token is indistinguishable to
# the orchestrator from a terminal `ci-failed`: the watcher exits, that IS the
# wake, and Step 2 dispatches a `ci-debugging` fix worker. On PR #1420 it did that
# at a head carrying three CONCLUDED GREEN runs; three immediate re-probes all
# read `ready` and the PR merged normally. So this case is the fix, not a
# refinement of it, and it is what makes the token non-terminal.
rc=0
out="$(TOKENS="ci-unreadable,ci-unreadable,ready" run_watch 121)" || rc=$?
check "ci-unreadable is in-flight; ready wakes" "WATCH 121 ready" "$out"
check "ci-unreadable busy-wake pin exits 0" "0" "$rc"
check "ci-unreadable busy-wake pin polled exactly 3 times (never woke early)" "3" \
  "$(polls 121)"

# THE SAFETY DIRECTION, and the answer to the only real objection to #1408: does
# a non-terminal token let a genuine failure HIDE? No. `ci-unreadable` is only
# ever the answer to a poll that could not be read; the moment one CAN be read and
# the tree really is red, pr-ready.sh says `ci-failed`, which is still terminal,
# so the watcher wakes and the lane is debugged. The new token can delay a fix
# worker by one poll interval. It cannot cancel one — and a token that could would
# be a worse bug than the one #1408 closes.
out="$(TOKENS="ci-unreadable,ci-failed" run_watch 122)"
check "a durable red surfaces on the next readable poll" "WATCH 122 ci-failed" "$out"
check "ci-unreadable → ci-failed case polled exactly twice" "2" "$(polls 122)"

# THE RESIDUAL RISK, AND WHAT BOUNDS IT (#1408 §6). The argument above rests on
# "the next poll can be read". If the rollup probe fails PERSISTENTLY — a dead
# token, a GitHub outage, a `--jq` that throws — every poll answers
# `ci-unreadable` and the lane never becomes terminal on its own. That is a stall
# traded for an over-dispatch, and a stall with no escalation would be a new
# failure mode rather than a fix.
#
# This is the assertion that bounds it: the watch times out like any other wait
# state, exits 0, and carries the token out with it as the distinct, greppable
# string `timeout ci-unreadable`. `.claude/commands/ralph-tick.md` carries the
# matching orchestrator policy for that string — that a persistently unreadable
# lane escalates rather than being re-watched forever — and test_pr_ready.sh's
# #1408 documentation block asserts the policy is written down. This case is what
# makes the string it routes on real.
rc=0
out="$(TOKENS="ci-unreadable" run_watch 123 0.2 1)" || rc=$?
check "ci-unreadable alone times out as a wait state" \
  "WATCH 123 timeout ci-unreadable" "$out"
check "ci-unreadable timeout exits 0" "0" "$rc"

# --- gone: a merged/closed PR ends the watch --------------------------------
rc=0
out="$(TOKENS="pending" STATES="OPEN,MERGED" run_watch 105)" || rc=$?
check "PR merging mid-watch → gone" "WATCH 105 gone" "$out"
check "gone exits 0" "0" "$rc"

out="$(TOKENS="ready" STATES="CLOSED" run_watch 106)"
check "already-closed PR → gone" "WATCH 106 gone" "$out"
check "closed PR never even polls pr-ready" "0" "$(polls 106)"

# --- timeout: exit 0, carrying the last classified token --------------------
rc=0
out="$(TOKENS="pending" run_watch 107 0.2 1)" || rc=$?
check "timeout prints last token" "WATCH 107 timeout pending" "$out"
check "timeout exits 0" "0" "$rc"

# Never classified at all (every poll a tooling failure) → `unknown`.
out="$(TOKENS="ERR" run_watch 108 0.2 1)"
check "timeout with no classification → unknown" "WATCH 108 timeout unknown" "$out"

# --- error tolerance: transient failures never kill the watcher -------------
rc=0
out="$(TOKENS="ERR,ERR,ready" run_watch 109)" || rc=$?
check "pr-ready failures are tolerated → ready" "WATCH 109 ready" "$out"
check "tolerated failures still exit 0" "0" "$rc"
check "kept polling through the failures" "3" "$(polls 109)"

out="$(TOKENS="ready" GH_STATE_EC=1 run_watch 110)"
check "failed gh state lookup is tolerated → ready" "WATCH 110 ready" "$out"

# --- pidfile idempotence ----------------------------------------------------
# A LIVE watcher (this test's own pid) already owns PR 111 → the duplicate
# reports `already-watching` without a single poll. This is what lets the
# orchestrator blindly (re)launch a watcher per in-flight PR on every wake.
slug_pidfile() { # slug_pidfile <PR> — the pidfile path watch-pr.sh derives in $WORK
  local slug
  slug="$(basename "$WORK" | tr -c 'A-Za-z0-9_-' '-' | tr -s '-' | sed 's/-$//')"
  printf '%s\n' "$PIDDIR/ralph-watch-$slug-$1.pid"
}

echo "$$" > "$(slug_pidfile 111)"
rc=0
out="$(TOKENS="ready" run_watch 111)" || rc=$?
check "live pidfile → already-watching" "WATCH 111 already-watching" "$out"
check "already-watching exits 0" "0" "$rc"
check "already-watching never polls" "0" "$(polls 111)"
check "live pidfile is left alone" "$$" "$(cat "$(slug_pidfile 111)")"
rm -f "$(slug_pidfile 111)"

# A STALE pidfile (its pid is dead) is taken over, and the watcher removes the
# pidfile again on exit — no permanent lock from a crashed session or reboot.
( : ) & dead_pid=$!
wait "$dead_pid" 2>/dev/null || true
echo "$dead_pid" > "$(slug_pidfile 112)"
out="$(TOKENS="ready" run_watch 112)"
check "stale (dead-pid) pidfile is taken over" "WATCH 112 ready" "$out"
if [[ -e "$(slug_pidfile 112)" ]]; then
  bad "pidfile removed on exit"
else
  ok "pidfile removed on exit"
fi

# Garbage in the pidfile is stale too — never a permanent already-watching.
echo "not-a-pid" > "$(slug_pidfile 113)"
out="$(TOKENS="ready" run_watch 113)"
check "garbage pidfile is taken over" "WATCH 113 ready" "$out"

# --- the watcher must not know any CI job name -----------------------------
# Issue #1141 renamed and split the CI jobs (`Code Quality & Testing` became
# `Tests & Type Checking`, plus new `Static Analysis` and `Pylint` jobs). The
# watcher survived that only because it classifies purely by the token
# pr-ready.sh prints and by `gh pr view --json state` — it never reads a job
# name. That is a property, not an accident, and a property nobody asserts is
# one a future edit can spend without noticing: a watcher that grepped for its
# own job name would, after a rename, see no CI at all and either spin until
# TIMEOUT or wake on a run it had not actually checked.
JOB_NAME_RE='Code Quality & Testing|Tests & Type Checking|CrawDad Quality|Quality Gate|Build Distribution|Code Complexity Analysis|Integration & E2E|Static Analysis'
hits="$(grep -vE '^\s*#' "$SRC" | grep -nE "$JOB_NAME_RE" || true)"
if [[ -z "$hits" ]]; then
  ok "watch-pr.sh hardcodes no CI job name"
else
  bad "watch-pr.sh hardcodes a CI job name: $hits"
fi

# And the same for the sibling it delegates every CI question to. pr-ready.sh
# keys off `gh pr checks`'s EXIT CODE rather than its table text (see the
# header there); a job-name literal creeping in would be the regression that
# header exists to prevent.
hits="$(grep -vE '^\s*#' "$(dirname "$SRC")/pr-ready.sh" | grep -nE "$JOB_NAME_RE" || true)"
if [[ -z "$hits" ]]; then
  ok "pr-ready.sh hardcodes no CI job name"
else
  bad "pr-ready.sh hardcodes a CI job name: $hits"
fi

# --- long holds are polled materially less often (#1197) -------------------
# `review-quota-exhausted` (#1160) held a lane for SEVEN DAYS on PR #1158, and
# `main-not-green` (#1159) for ~20 minutes. Nothing the orchestrator does
# shortens either. Each pr-ready.sh poll costs ~8-10 gh calls, so one held lane
# at 30s is ~1,000 calls/hour against a 5,000/hour REST budget — burning the
# API budget hardest exactly when nothing can merge.
#
# The back-off is a MULTIPLIER of the caller's interval, not an absolute floor
# in seconds: this suite drives the watcher at fractional intervals to stay
# fast, and a "back off to 5 minutes" floor would hang every case below.
#
# Asserted by stubbing `sleep` and reading the interval the watcher actually
# CHOSE, rather than by counting polls in wall-clock. Poll counts are dominated
# by subprocess overhead (~0.2s per iteration against a 0.05s interval), which
# makes a count-ratio assertion flaky in exactly the direction that hides a
# regression. The chosen interval is the thing under test; measure that.
cat > "$BIN/sleep" <<'STUB'
#!/usr/bin/env bash
printf '%s\n' "$1" >> "$STATE_DIR/sleeps"
exec /bin/sleep 0.01
STUB
chmod +x "$BIN/sleep"

slept() { sort -u "$STATE/case-$1/sleeps" 2>/dev/null | tr '\n' ' ' | sed 's/ $//'; }

TOKENS="pending" run_watch 130 0.5 1 >/dev/null
TOKENS="review-quota-exhausted" run_watch 131 0.5 1 >/dev/null
TOKENS="main-not-green" run_watch 132 0.5 1 >/dev/null
TOKENS="awaiting-review" run_watch 134 0.5 1 >/dev/null

# Positive control: without it, a watcher that never slept at all would make
# every "backed off" assertion below vacuously true.
check "a CI-speed wait sleeps exactly the interval it was given" "0.5" "$(slept 130)"
check "awaiting-review is a CI-speed wait too" "0.5" "$(slept 134)"

check "review-quota-exhausted sleeps 10x the interval" "5" "$(slept 131)"
check "main-not-green sleeps 10x the interval" "5" "$(slept 132)"

rm -f "$BIN/sleep"

# The back-off must change only HOW OFTEN the question is asked, never which
# token wakes the orchestrator (the issue's second acceptance criterion).
out="$(TOKENS="review-quota-exhausted,ready" run_watch 133 0.05 30)"
check "a backed-off lane still wakes on the next actionable token" \
  "WATCH 133 ready" "$out"

# --- summary ---------------------------------------------------------------
echo
echo "watch-pr tests: $PASS passed, $FAIL failed"
[[ "$FAIL" -eq 0 ]]
