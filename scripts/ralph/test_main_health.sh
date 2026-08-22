#!/usr/bin/env bash
# scripts/ralph/test_main_health.sh
#
# Offline tests for main-health.sh — the `main` CI circuit breaker (issue #1159).
#
# WHY IT EXISTS: PR #1157 replaced a strict pre-merge freshness gate with a
# RISK-BASED one — a lane that is behind `main` still merges when the two
# changesets provably cannot interact (pr-ready.sh's RISK_SURFACE_RE plus
# merge-base disjointness). The justification for that relaxation is written
# into pr-ready.sh's own header: "what backstops the residual risk is the full
# CI run on `push: main` — every squash-merge re-proves the merged result, so a
# stale green that slips through is caught on `main` rather than assumed away."
# Nothing in the loop had ever READ that run's conclusion, so the backstop was
# an assumption rather than a check. This script reads it; these tests pin what
# it may and may not conclude from what it reads.
#
# CONTRACT (identical in spirit to pr-ready.sh's): exactly one token on stdout —
# green | red | pending | unknown — and exit 0 on every one of them. A non-zero
# exit (2) is a usage/tooling error, NEVER a verdict about `main`. Everything is
# offline: a fake, arg-aware `gh` on PATH scripts the `gh run list` answer.
#
# The dimensions pinned here:
#
#   conclusions    completed/success → green; completed/{failure, timed_out,
#                  startup_failure} → red. cancelled / skipped / neutral /
#                  action_required / stale / an EMPTY conclusion are not
#                  evidence about `main` at all — the walk skips them and keeps
#                  going, because "somebody cancelled a run" says nothing about
#                  whether the tree builds.
#   anti-serialization  the walk keys off the newest CONCLUSIVE run, never the
#                  newest run. `main` CI lags each merge by ~14 minutes, so at
#                  any moment the newest run is usually still in flight; keying
#                  off it would hold every behind lane for a quarter of an hour
#                  after every merge — reintroducing exactly the serialization
#                  #1138 removed. This gate is a circuit breaker, not a barrier.
#   fail closed    an empty list, a non-zero gh exit, unparseable output, a
#                  surplus 6th field, or a window of nothing but inconclusive
#                  runs all read `unknown` — and `unknown` is never `green`.
#                  An unreadable answer is not permission. A malformed line
#                  stops the walk and nothing more: it never CREATES a verdict
#                  (above a green it still reads `unknown`, never `green`) and
#                  never UPGRADES one, so it also may not discard a `red` a
#                  well-formed line above it already proved — downgrading that
#                  to `unknown` is lossy, not safe.
#   cost           exactly ONE gh call on every path. This runs per behind lane
#                  per wake; a second call here is a rate limit there.
#   stdout purity  stdout is the token and nothing else, always, so a caller can
#                  write `tok="$(main-health.sh)"` and compare it directly.
#                  Attribution goes to stderr.
#   attribution    the newest RED run is not necessarily the run that BROKE
#                  `main`: with several merges landing close together the
#                  culprit is somewhere in `<newest green sha>..<red sha>`, so
#                  that RANGE is what stderr must carry (the issue asks for a
#                  commit, not a run id). With no green run in the window the
#                  culprit is genuinely unrecoverable and must be SAID — never
#                  faked as a range with an empty left side.
#
# Plus one cross-file coupling check, the same silent-wedge class as
# test_pr_ready.sh's `claude-review` job-key assertions: `.github/workflows/ci.yml`
# must keep a `push:` trigger that includes `main`, and its
# `concurrency.cancel-in-progress` must not become an unconditional `true`.
# Either edit deletes the very `push: main` run this gate reads, silently, with
# nothing else in the repo failing.
#
# Run:  bash scripts/ralph/test_main_health.sh
set -euo pipefail

HEALTH="$(cd "$(dirname "$0")" && pwd)/main-health.sh"
PASS=0
FAIL=0

ok()  { PASS=$((PASS + 1)); printf '  ok  - %s\n' "$1"; }
bad() { FAIL=$((FAIL + 1)); printf 'FAIL  - %s\n' "$1"; }
check() { # check <desc> <expected> <actual>
  if [[ "$2" == "$3" ]]; then ok "$1"; else bad "$1 (expected '$2', got '$3')"; fi
}
not_green() { # not_green <desc> <token> — the cardinal rule, in one helper
  if [[ "$2" != "green" ]]; then ok "$1"; else bad "$1 (got 'green')"; fi
}
one_token() { # one_token <desc> <stdout> — one whitespace-free token, nothing else
  case "$2" in
    green|red|pending|unknown) ok "$1" ;;
    *) bad "$1 (stdout was '$2', not a single bare token)" ;;
  esac
}
contains() { # contains <desc> <needle> <haystack>
  if [[ "$3" == *"$2"* ]]; then ok "$1"; else bad "$1 (no '$2' in: $3)"; fi
}
lacks() { # lacks <desc> <needle> <haystack>
  if [[ "$3" != *"$2"* ]]; then ok "$1"; else bad "$1 (unexpected '$2' in: $3)"; fi
}

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
BIN="$WORK/bin"
mkdir -p "$BIN"

# Arg-aware fake gh. main-health.sh makes exactly ONE call —
#   gh run list --workflow ci.yml --branch main --limit 20 \
#     --json status,conclusion,headSha,databaseId,url --jq '<join to lines>'
# — so this stub has one real arm, driven by env vars the tests set per case:
#   RUNS      — the already-extracted answer, one run per line, NEWEST FIRST,
#               five `|`-separated fields (status|conclusion|headSha|id|url).
#               Defaulted with `-` (not `:-`) so `RUNS=''` reproduces the empty
#               run list, which must fail closed rather than read as healthy.
#   RUNS_EC   — exit code of the call; a test sets 1 to prove a failed lookup
#               yields `unknown`, never the happy answer sitting on stdout.
#   RUNS_JSON — raw `--json …` payload; when set, the stub runs the REAL jq with
#               main-health.sh's OWN `--jq` expression against it (the
#               COMMENTS_JSON/ROLLUP_JSON pattern from test_pr_ready.sh), so a
#               jq that drops a field, mis-orders them, or trips over
#               `databaseId` being a NUMBER and `conclusion` being JSON null is
#               caught here — a scalar stub would mask all three.
#   GH_CALLS  — file the stub appends one line to per invocation; the cost
#               assertions read it, because one wasted request per behind lane
#               per wake is how a loop burns its rate limit.
#   GH_ARGS   — file the stub appends its whole argv to, so the query contract
#               (workflow, branch, limit, fields, --repo) is asserted directly
#               rather than inferred from the answer.
cat > "$BIN/gh" <<'STUB'
#!/usr/bin/env bash
args="$*"
[[ -z "${GH_CALLS:-}" ]] || printf 'call\n' >> "$GH_CALLS"
[[ -z "${GH_ARGS:-}" ]] || printf '%s\n' "$args" >> "$GH_ARGS"
case "$args" in
  "run list"*)
    if [[ -n "${RUNS_JSON:-}" ]]; then
      expr="" prev=""
      for a in "$@"; do [[ "$prev" == "--jq" ]] && expr="$a"; prev="$a"; done
      printf '%s' "$RUNS_JSON" | jq -r "$expr"
      exit "${RUNS_EC:-0}"
    fi
    [[ -z "${RUNS-}" ]] || printf '%s\n' "$RUNS"
    exit "${RUNS_EC:-0}" ;;
  *) echo '' ;;
esac
STUB
chmod +x "$BIN/gh"

# stdout only — the shape every caller of this script uses. `${1+"$@"}` (not a
# bare `"$@"`) because the normal invocation has NO arguments at all, and an
# empty `"$@"` trips `set -u` on bash 3.2, the stock /bin/bash on macOS — the
# same portability guard pr-ready.sh documents at its `repo_args` expansion.
run() { PATH="$BIN:$PATH" "$HEALTH" ${1+"$@"} 2>/dev/null; }
# Same, but stderr is kept: the attribution assertions need to read it, and the
# stdout-purity assertions need it OUT of the way to prove stdout is one token.
run_capture() { # run_capture <stderr file> [args…]
  local errfile="$1"
  shift
  PATH="$BIN:$PATH" "$HEALTH" ${1+"$@"} 2>"$errfile"
}
calls() { # calls <counter file> — how many times the gh stub ran
  if [[ -f "$1" ]]; then grep -c . "$1" || true; else echo 0; fi
}
r() { # r <status> <conclusion> <sha> <id> — one `gh run list` line, five fields
  printf '%s|%s|%s|%s|https://x/%s' "$1" "$2" "$3" "$4" "$4"
}

SHA_GREEN="abc1234"
# No digits in the red sha on purpose: the stderr assertions below look for the
# run ID `12` as a substring, and a sha containing "12" would satisfy that check
# without the id ever being printed.
SHA_RED="deadbee"
GREEN_RUN="$(r completed success "$SHA_GREEN" 11)"
RED_RUN="$(r completed failure "$SHA_RED" 12)"
FLIGHT_RUN="$(r in_progress "" feed1234 13)"
QUEUED_RUN="$(r queued "" feed5678 14)"

# --- usage errors exit 2, and are the ONLY non-zero exits -------------------
rc=0
PATH="$BIN:$PATH" "$HEALTH" --bogus >/dev/null 2>&1 || rc=$?
check "unknown option exits 2" "2" "$rc"

rc=0
PATH="$BIN:$PATH" "$HEALTH" --repo >/dev/null 2>&1 || rc=$?
check "--repo with no value exits 2" "2" "$rc"

# There are no positional arguments at all — this script asks about `main`, not
# about a PR. A stray number is a caller confusing it with pr-ready.sh, and
# silently ignoring it would answer a question nobody asked.
rc=0
PATH="$BIN:$PATH" "$HEALTH" 100 >/dev/null 2>&1 || rc=$?
check "unexpected positional argument exits 2" "2" "$rc"

# Non-vacuity for the three above: the no-argument invocation is the normal one
# and must be a plain exit-0 verdict, so those exits are provably about the
# arguments and not about the script refusing to run at all.
rc=0
out="$(RUNS="$GREEN_RUN" run)" || rc=$?
check "no arguments → a verdict" "green" "$out"
check "no arguments → exit 0" "0" "$rc"

# --- conclusions: what counts as evidence about `main` ----------------------
check "completed/success → green" "green" "$(RUNS="$GREEN_RUN" run)"
check "completed/failure → red" "red" "$(RUNS="$RED_RUN" run)"

# A run that ran out of wall clock or died before the first step never proved
# the tree builds, and the backstop's whole job is to notice that.
check "completed/timed_out → red" "red" \
  "$(RUNS="$(r completed timed_out "$SHA_RED" 15)" run)"
check "completed/startup_failure → red" "red" \
  "$(RUNS="$(r completed startup_failure "$SHA_RED" 16)" run)"

# `red` is a VERDICT, not a tooling error: the caller reads the token, and a
# non-zero exit would be indistinguishable from "the script is broken".
rc=0
out="$(RUNS="$RED_RUN" run)" || rc=$?
check "red is a verdict, not an error → exit 0" "0" "$rc"
check "red verdict token" "red" "$out"

# --- not-evidence: inconclusive runs are SKIPPED, not answered on -----------
# A cancelled/skipped/neutral/action_required/stale run — or one whose
# conclusion field is empty — says nothing about whether `main` builds. Reading
# any of them as "not red" would clear the gate on no evidence; reading them as
# "not green" would wedge every behind lane the first time somebody cancels a
# run. The only correct move is to keep walking to the next run.
INCONCLUSIVE_OVER_RED="$(r completed skipped c0ffee1 20)
$RED_RUN"
check "newest completed/skipped over an older failure → red" "red" \
  "$(RUNS="$INCONCLUSIVE_OVER_RED" run)"

for inconclusive in neutral action_required stale ""; do
  payload="$(r completed "$inconclusive" c0ffee1 20)
$RED_RUN"
  check "newest completed/${inconclusive:-<empty>} over an older failure → red" "red" \
    "$(RUNS="$payload" run)"
done

# The mirror image, so the skipping above is provably about "not evidence" and
# not about a bias toward `red`: a cancelled run over a green one is green.
CANCELLED_OVER_GREEN="$(r completed cancelled c0ffee1 21)
$GREEN_RUN"
check "newest completed/cancelled over an older success → green" "green" \
  "$(RUNS="$CANCELLED_OVER_GREEN" run)"

# --- ANTI-SERIALIZATION PIN (issue #1159 constraint 4) ----------------------
# `main` CI lags each merge by ~14 minutes, so the newest run on `main` is very
# often still in flight — that is the STEADY STATE of a busy fleet, not an
# exception. Keying this gate off the newest run would therefore hold every
# behind lane for a full CI round after every single merge, which is precisely
# the serialization #1138 removed and #1157 was written to keep removed. Keying
# off the newest CONCLUSIVE run instead keeps this a circuit breaker (it trips
# only on proven breakage) rather than a barrier (it trips on every merge).
IN_FLIGHT_OVER_GREEN="$FLIGHT_RUN
$GREEN_RUN"
check "run in flight over a completed success → green (anti-serialization pin)" "green" \
  "$(RUNS="$IN_FLIGHT_OVER_GREEN" run)"

# The same pin in the direction that actually matters — this is what makes the
# gate a circuit BREAKER rather than a barrier: a run in flight ABOVE a
# conclusive failure must still report `red` immediately, not `pending`. The
# decided verdict wins over the trailing in-flight fallback. If it did not, the
# breaker could never trip on a busy fleet at all: the newest run on `main` is
# nearly always in flight, so every merge landing after `main` broke would
# refresh the `pending` answer and the loop would go on stacking unvalidated
# merges onto a broken tree for as long as merges kept arriving — the exact
# failure #1159 exists to stop. This passes against the implementation as
# written; it is here so a future refactor of the walk cannot silently turn the
# breaker back into a barrier.
IN_FLIGHT_OVER_RED="$FLIGHT_RUN
$RED_RUN"
check "run in flight over a completed failure → red, not pending (circuit-breaker pin)" "red" \
  "$(RUNS="$IN_FLIGHT_OVER_RED" run)"

# --- pending: something is in flight and NOTHING has concluded --------------
check "only an in-flight run → pending" "pending" "$(RUNS="$FLIGHT_RUN" run)"
check "only a queued run → pending" "pending" "$(RUNS="$QUEUED_RUN" run)"

# In flight over nothing but an inconclusive run is still "no evidence yet, but
# evidence is coming" — the one state where waiting is genuinely the answer.
FLIGHT_OVER_INCONCLUSIVE="$FLIGHT_RUN
$(r completed cancelled c0ffee1 22)"
check "in flight over an inconclusive run → pending" "pending" \
  "$(RUNS="$FLIGHT_OVER_INCONCLUSIVE" run)"

# --- unknown: every answer we cannot use -----------------------------------
ALL_INCONCLUSIVE="$(r completed cancelled c0ffee1 23)
$(r completed skipped c0ffee2 24)"

check "empty run list → unknown" "unknown" "$(RUNS="" run)"
# gh printing a happy answer AND exiting non-zero is the dangerous shape: the
# output is already on stdout, so an implementation that forgets to check the
# exit code inherits it and clears the gate on a failed lookup.
check "gh non-zero exit → unknown" "unknown" "$(RUNS="$GREEN_RUN" RUNS_EC=1 run)"
check "unparseable output → unknown" "unknown" "$(RUNS="not a run line at all" run)"
# A surplus 6th field means a `|` appeared where none legitimately can (a status
# enum, a conclusion enum, a sha, an integer id and a URL can none of them carry
# one). Same fail-closed rule as pr-ready.sh:323 / :370 / :483 — a shifted field
# must never become a verdict.
check "surplus 6th field → unknown" "unknown" "$(RUNS="$GREEN_RUN|extra" run)"
check "all runs inconclusive, none in flight → unknown" "unknown" \
  "$(RUNS="$ALL_INCONCLUSIVE" run)"

# --- CARDINAL FAIL-CLOSED SWEEP ---------------------------------------------
# The operator's directive for this gate is "an unreadable answer is not
# permission". This sweep is that directive in executable form: every garbage
# and failure input above (plus the shapes no single case above covers) run
# through three assertions at once — the token is NEVER `green`, the exit code
# is ALWAYS 0, and stdout is ALWAYS exactly one whitespace-free known token.
# Individually the cases pin which token; together they pin the property that
# no future refactor can turn any unreadable answer into a merge.
SWEEP_DESCS=()
SWEEP_RUNS=()
SWEEP_ECS=()
add_case() { SWEEP_DESCS+=("$1"); SWEEP_RUNS+=("$2"); SWEEP_ECS+=("${3:-0}"); }

add_case "empty run list" ""
add_case "whitespace-only output" "   "
add_case "gh exits non-zero over a green answer" "$GREEN_RUN" 1
add_case "gh exits non-zero over nothing" "" 1
add_case "unparseable garbage" "not a run line at all"
add_case "raw JSON leaking through a failed --jq" '{"status":"completed"}'
add_case "surplus 6th field" "$GREEN_RUN|extra"
add_case "truncated line (three fields)" "completed|success|$SHA_GREEN"
add_case "an unrecognised status word" "$(r finished success "$SHA_GREEN" 25)"
add_case "all runs inconclusive, none in flight" "$ALL_INCONCLUSIVE"

for i in "${!SWEEP_DESCS[@]}"; do
  desc="${SWEEP_DESCS[i]}"
  rc=0
  out="$(RUNS="${SWEEP_RUNS[i]}" RUNS_EC="${SWEEP_ECS[i]}" run)" || rc=$?
  not_green "fail closed: $desc is never green" "$out"
  check "fail closed: $desc still exits 0" "0" "$rc"
  one_token "fail closed: $desc prints one bare token" "$out"
done

# --- cost: exactly ONE gh call on EVERY path --------------------------------
# pr-ready.sh calls this per behind lane per wake. Two calls here doubles the
# gate's cost across the whole fleet, and the fleet is what runs out of API
# budget first. Each path gets its OWN counter file — a shared one would let an
# earlier case's call satisfy a later case's assertion.
C_GREEN="$WORK/calls-green"
tok="$(GH_CALLS="$C_GREEN" RUNS="$GREEN_RUN" run)" || tok="exit-$?"
check "cost: green path token" "green" "$tok"
check "cost: green path makes exactly one gh call" "1" "$(calls "$C_GREEN")"

C_RED="$WORK/calls-red"
tok="$(GH_CALLS="$C_RED" RUNS="$RED_RUN" run)" || tok="exit-$?"
check "cost: red path token" "red" "$tok"
check "cost: red path makes exactly one gh call" "1" "$(calls "$C_RED")"

C_PENDING="$WORK/calls-pending"
tok="$(GH_CALLS="$C_PENDING" RUNS="$FLIGHT_RUN" run)" || tok="exit-$?"
check "cost: pending path token" "pending" "$tok"
check "cost: pending path makes exactly one gh call" "1" "$(calls "$C_PENDING")"

# The unknown path is the one a retry loop would be tempting on — and a retry
# loop is how one failed lookup becomes N requests on every lane at once.
C_UNKNOWN="$WORK/calls-unknown"
tok="$(GH_CALLS="$C_UNKNOWN" RUNS="$GREEN_RUN" RUNS_EC=1 run)" || tok="exit-$?"
check "cost: unknown path token" "unknown" "$tok"
check "cost: unknown path makes exactly one gh call (no retry)" "1" "$(calls "$C_UNKNOWN")"

# --- stdout purity: the token, and nothing but the token --------------------
# The caller does `tok="$(main-health.sh --repo …)"` and compares the result to
# `green` directly. One stray informational line on stdout turns every
# comparison false, i.e. holds the whole fleet — so attribution lives on stderr
# and stdout stays a single token on every path.
E_GREEN="$WORK/err-green"
out="$(RUNS="$GREEN_RUN" run_capture "$E_GREEN")" || out="exit-$?"
check "green stdout is the bare token" "green" "$out"
if [[ -s "$E_GREEN" ]]; then
  bad "a healthy main says nothing on stderr (got: $(cat "$E_GREEN"))"
else
  ok "a healthy main says nothing on stderr"
fi

E_RED="$WORK/err-red"
out="$(RUNS="$RED_RUN" run_capture "$E_RED")" || out="exit-$?"
check "red stdout is the bare token" "red" "$out"
red_err="$(cat "$E_RED" 2>/dev/null || true)"
if [[ -s "$E_RED" ]]; then
  ok "a red main attributes on stderr"
else
  bad "a red main must say WHAT is red on stderr, or the operator has to go hunting"
fi
contains "red stderr names the failing run id" "12" "$red_err"
contains "red stderr names the failing run url" "https://x/12" "$red_err"
contains "red stderr names the failing headSha" "$SHA_RED" "$red_err"

E_PENDING="$WORK/err-pending"
out="$(RUNS="$FLIGHT_RUN" run_capture "$E_PENDING")" || out="exit-$?"
check "pending stdout is the bare token" "pending" "$out"

E_UNKNOWN="$WORK/err-unknown"
out="$(RUNS="$GREEN_RUN|extra" run_capture "$E_UNKNOWN")" || out="exit-$?"
check "unknown stdout is the bare token" "unknown" "$out"

# --- attribution: a RANGE, because a run id is not a commit -----------------
# The issue asks for the COMMIT that broke `main`, and the newest red run is not
# it. Merges land minutes apart while a CI round takes ~14, so by the time the
# first red run reports, two or three more commits are already on `main` and
# several of their runs are red too. All that is actually PROVEN is: the tree
# was good at the newest green run's sha and bad at the red one's, so the
# culprit is somewhere in `<newest green sha>..<red sha>`. Printing the red
# run's sha alone would send whoever reads this line to the wrong commit.
SHA_A="a11a11a"
SHA_B="b22b22b"
SHA_C="c33c33c"
BLAME_WINDOW="$(r completed failure "$SHA_C" 33)
$(r completed failure "$SHA_B" 32)
$(r completed success "$SHA_A" 31)"
E_BLAME="$WORK/err-blame"
out="$(RUNS="$BLAME_WINDOW" run_capture "$E_BLAME")" || out="exit-$?"
check "blame window token" "red" "$out"
blame_err="$(cat "$E_BLAME" 2>/dev/null || true)"
contains "blame range spans newest green .. the red run" "$SHA_A..$SHA_C" "$blame_err"
lacks "blame does NOT narrow to the newest red run alone" "$SHA_B..$SHA_C" "$blame_err"

# No green run anywhere in the window ⇒ there is no lower bound, so there is no
# honest range. Say so. Fabricating one (`..<red sha>`, or a range rooted at an
# empty string) would point a human at every commit in the repo's history and
# read, to a script, exactly like a real answer.
NO_GREEN_WINDOW="$(r completed failure "$SHA_C" 43)
$(r completed failure "$SHA_B" 42)"
E_NOGREEN="$WORK/err-nogreen"
out="$(RUNS="$NO_GREEN_WINDOW" run_capture "$E_NOGREEN")" || out="exit-$?"
check "no-green window is still red" "red" "$out"
nogreen_err="$(cat "$E_NOGREEN" 2>/dev/null || true)"
lacks "no fabricated range with an empty left side" "..$SHA_C" "$nogreen_err"
if grep -Eqi 'no green run|unattributable|unrecoverable' <<<"$nogreen_err"; then
  ok "no-green window SAYS the culprit is unrecoverable"
else
  bad "no-green window must say the culprit is unrecoverable (stderr was: $nogreen_err)"
fi

# --- a malformed line never CREATES or UPGRADES a verdict -------------------
# Two cases, one rule: a malformed line STOPS THE WALK, and stopping the walk is
# ALL it does. It may not invent a verdict, and it may not discard one a
# well-formed line already PROVED. The only answer it can ever produce on its
# own is `unknown`.
#
# Case one — a malformed line encountered BELOW a decided `red`. At that point
# the walk is no longer deciding anything: the verdict is settled and it is only
# hunting older runs for the newest green, the floor of the blame range.
# Throwing the proven red away there and printing `unknown` instead is LOSSY,
# not safe. pr-ready.sh holds a lane identically on `red` and on `unknown` (only
# `green` clears the gate), so there is no merge-safety difference whatsoever —
# but `.claude/commands/ralph-tick.md`'s Step 0b dispatches the `ci-debugging`
# worker and reports the blame range ONLY on the literal `red` token. The
# downgrade therefore silently disables the auto-remediation path at exactly the
# moment `main` is broken. Keep the red; lose only the floor.
SHA_MALFORMED="d44d44d"
MALFORMED_BELOW_RED="$(r completed failure "$SHA_C" 53)
$(r completed success "$SHA_MALFORMED" 52)|extra
$(r completed success "$SHA_A" 51)"
E_MALF_RED="$WORK/err-malformed-below-red"
rc=0
out="$(RUNS="$MALFORMED_BELOW_RED" run_capture "$E_MALF_RED")" || rc=$?
check "malformed line BELOW a proven failure keeps the red verdict" "red" "$out"
check "malformed line below red still exits 0" "0" "$rc"
one_token "malformed line below red prints one bare token" "$out"
malf_red_err="$(cat "$E_MALF_RED" 2>/dev/null || true)"
contains "red attribution survives a malformed line below it" "https://x/53" "$malf_red_err"

# The walk still STOPPED at that malformed line, so the green two lines further
# down was never reached and there is no proven floor. That is the same state as
# a window holding no green at all, and it gets the same honest answer: say the
# culprit is unattributable rather than rooting a range at an empty string, at
# the very line we just refused to trust, or at a green we never actually read.
lacks "no blame range with an empty left side after a malformed line" \
  "..$SHA_C" "$malf_red_err"
lacks "the never-reached green below the malformed line is not fabricated into a floor" \
  "$SHA_A" "$malf_red_err"
if grep -Eqi 'no green run|unattributable|unrecoverable' <<<"$malf_red_err"; then
  ok "malformed line below red SAYS the culprit is unattributable"
else
  bad "malformed line below red must say the culprit is unattributable (stderr was: $malf_red_err)"
fi

# Case two, the mirror — and the reason the rule is "never creates, never
# upgrades" rather than "never stops". A malformed line ABOVE a legitimate
# green, i.e. while the verdict is still UNDECIDED, must still yield `unknown`.
# Skipping it and walking on would resolve to `green`: the one answer that lets
# a merge onto a tree whose real state we have just admitted we cannot read.
# This already passes and must keep passing — case one may NOT be implemented by
# relaxing the malformed check into a plain `continue`, only by preserving a
# verdict that was already decided before the malformed line was reached.
MALFORMED_ABOVE_GREEN="$(r completed success "$SHA_MALFORMED" 62)|extra
$GREEN_RUN"
rc=0
out="$(RUNS="$MALFORMED_ABOVE_GREEN" run)" || rc=$?
check "malformed line ABOVE a legitimate green → unknown" "unknown" "$out"
not_green "malformed line above a legitimate green is never green" "$out"
check "malformed line above green still exits 0" "0" "$rc"
one_token "malformed line above green prints one bare token" "$out"

# --- the query contract, asserted on the argv itself ------------------------
# Reading the token alone cannot tell a `--workflow ci.yml --branch main` query
# apart from one that quietly widened to every workflow on every branch (which
# would answer about a lane's OWN CI, not about `main`). The stub records its
# argv so the query is pinned directly.
A_DEFAULT="$WORK/gh-args-default"
tok="$(GH_ARGS="$A_DEFAULT" RUNS="$GREEN_RUN" run)" || tok="exit-$?"
check "default invocation token" "green" "$tok"
argv="$(cat "$A_DEFAULT" 2>/dev/null || true)"
contains "the one gh call is a ci.yml run list" "run list --workflow ci.yml" "$argv"
contains "the run list is scoped to the main branch" "--branch main" "$argv"
contains "the run list is windowed" "--limit 20" "$argv"
contains "the run list asks for all five fields" \
  "--json status,conclusion,headSha,databaseId,url" "$argv"
lacks "no --repo is invented when none was given" "--repo" "$argv"

# `--repo` must survive to the argv: pr-ready.sh forwards its own, and a helper
# that dropped it would answer about whatever repo the cwd happened to be in.
A_REPO="$WORK/gh-args-repo"
tok="$(GH_ARGS="$A_REPO" RUNS="$GREEN_RUN" run --repo owner/name)" || tok="exit-$?"
check "--repo lane still answers" "green" "$tok"
argv="$(cat "$A_REPO" 2>/dev/null || true)"
contains "--repo reaches the gh argv" "--repo owner/name" "$argv"

# --- REAL jq: exercise the production run-list expression -------------------
# The scalar stub above cannot catch a `--jq` that drops a field, mis-orders
# them, or trips over the two type surprises in this payload: `databaseId` is a
# NUMBER (hence `| tostring`) and `conclusion` is JSON null on a run still in
# flight (hence `// ""`). These cases feed raw payloads through main-health.sh's
# OWN expression, so a broken one fails here rather than in production.
if command -v jq >/dev/null 2>&1; then
  RJ_SUCCESS='{"status":"completed","conclusion":"success","headSha":"abc1234","databaseId":11,"url":"https://x/11"}'
  RJ_FAILURE='{"status":"completed","conclusion":"failure","headSha":"dead1234","databaseId":12,"url":"https://x/12"}'
  RJ_FLIGHT='{"status":"in_progress","conclusion":null,"headSha":"feed1234","databaseId":13,"url":"https://x/13"}'

  check "real payload: completed/success → green" "green" \
    "$(RUNS_JSON="[$RJ_SUCCESS]" run)"
  check "real payload: completed/failure → red" "red" \
    "$(RUNS_JSON="[$RJ_FAILURE]" run)"
  check "real payload: null conclusion in flight → pending" "pending" \
    "$(RUNS_JSON="[$RJ_FLIGHT]" run)"
  check "real payload: in flight over an older success → green" "green" \
    "$(RUNS_JSON="[$RJ_FLIGHT,$RJ_SUCCESS]" run)"
  check "real payload: empty array → unknown" "unknown" "$(RUNS_JSON='[]' run)"

  # The whole point of `| tostring` on databaseId: without it jq errors and gh
  # exits non-zero, which must read as `unknown` rather than as anything else.
  E_RJ_RED="$WORK/err-realjq-red"
  out="$(RUNS_JSON="[$RJ_FAILURE,$RJ_SUCCESS]" run_capture "$E_RJ_RED")" || out="exit-$?"
  check "real payload: red over green token" "red" "$out"
  contains "real payload: blame range survives the real jq" "abc1234..dead1234" \
    "$(cat "$E_RJ_RED" 2>/dev/null || true)"
else
  echo "  skip - real-jq run-list cases (jq not installed)"
fi

# --- cross-file coupling: the backstop must keep existing -------------------
# main-health.sh reads the conclusion of `ci.yml`'s `push: main` runs. Two edits
# to ci.yml would delete that evidence with nothing else in the repo failing:
# dropping the `push:` trigger (or its `main` branch), and making
# `cancel-in-progress` an unconditional `true` — which would let each new merge
# cancel the previous merge's validation run, so the backstop #1157's relaxation
# leans on would report `cancelled` (not evidence) forever. Same silent-wedge
# class as test_pr_ready.sh's `claude-review` job-key assertions.
CI_WORKFLOW="$(cd "$(dirname "$0")/../.." && pwd)/.github/workflows/ci.yml"

# The `push:` block's own keys, one level in: stop at the next top-level trigger
# (a 2-space key), so `pull_request:`'s branches can never satisfy this.
push_block="$(awk '$0 == "  push:" {p = 1; next}
                   p && /^  [^[:space:]#]/ {exit}
                   p {print}' "$CI_WORKFLOW" 2>/dev/null || true)"

if [[ -n "$push_block" ]]; then
  ok "ci.yml still declares a push: trigger"
else
  bad "ci.yml has no push: trigger — the run main-health.sh reads would not exist"
fi

# Word-bounded so `maintenance` or `main-docs` cannot pass for `main`.
if grep -Eq '(^|[^A-Za-z0-9_/-])main([^A-Za-z0-9_/-]|$)' <<<"$push_block"; then
  ok "ci.yml's push: trigger still includes main"
else
  bad "ci.yml's push: trigger no longer lists main (block was: $push_block)"
fi

cancel_line="$(grep -E '^[[:space:]]*cancel-in-progress:' "$CI_WORKFLOW" || true)"
cancel_value="${cancel_line#*:}"
cancel_value="$(printf '%s' "$cancel_value" | tr -d " \"'")"
if [[ "$cancel_value" == "true" ]]; then
  bad "ci.yml sets cancel-in-progress: true unconditionally — push: main runs would be cancelled"
else
  ok "ci.yml does not cancel push: main runs unconditionally"
fi

# --- summary ---------------------------------------------------------------
echo
echo "main-health tests: $PASS passed, $FAIL failed"
[[ "$FAIL" -eq 0 ]]
