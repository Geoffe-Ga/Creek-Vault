#!/usr/bin/env bash
# scripts/ralph/review-quota.sh
#
# Can the `claude-review` reviewer review RIGHT NOW? Prints exactly ONE token on
# stdout and exits 0 — the same query contract as pr-ready.sh and main-health.sh,
# so a caller can write `tok="$(review-quota.sh)"` and compare it directly. A
# non-zero exit (2) is a usage/tooling error, NEVER a verdict about the reviewer.
#
#   available  the reviewer works: the newest CONCLUSIVE code-review.yml run
#              succeeded, or it failed for a reason that is provably not the rate
#              limit (including a rejection whose window has already reset)
#   exhausted  PROVEN out of quota — see the three-part conjunction below
#   unknown    we could not read an answer we are willing to act on
#
# ---------------------------------------------------------------------------
# WHY THIS EXISTS (issue #1160)
# ---------------------------------------------------------------------------
# pr-ready.sh prints `behind` for a lane that carries a FRESH `Verdict: LGTM`
# plus green CI but sits behind `main` for a real reason (a lockfile bump, an
# overlapping file). ralph-tick.md's remedy for `behind` is `fleet.sh sync`,
# which pushes a merge commit — and that advances HEAD, which invalidates the
# LGTM under pr-ready.sh's own stale-verdict guard. Normally that costs one
# re-review and nothing else.
#
# THE FIX IS NOT "NEVER DESTROY THE LGTM ON SYNC". It is "NEVER DESTROY IT AT A
# MOMENT WHEN IT CANNOT BE REPLACED". When the `claude-review` quota is exhausted
# the re-review cannot happen, so the sync permanently destroys the only verdict
# the lane will ever get. Observed on PR #1158: LGTM at 05:11:43Z, sync at
# 05:23:58Z, re-review rejected in 24 seconds against a `seven_day` window that
# would not reset for three days. The lane was unmergeable for those three days,
# and the thing that made it unmergeable was the loop's own remedy.
#
# This script answers the one question that distinguishes the two situations, so
# pr-ready.sh can spend it as a PRECONDITION ON THE REMEDY — exactly the shape
# main-health.sh established for #1159. It is NOT a new merge gate, NOT a
# relaxation of any existing one, and it never forces a sync.
#
# ---------------------------------------------------------------------------
# THE INVERTED FAIL-CLOSED POLARITY — the single most important property here
# ---------------------------------------------------------------------------
# main-health.sh: anything that is not `green` HOLDS the lane.
# review-quota.sh: only a positively-proven `exhausted` HOLDS the lane.
#
# Both are fail-closed in the IDENTICAL sense — prefer the recoverable error —
# and therefore take OPPOSITE actions, because the recoverable error is the
# opposite one:
#   * main-health: a false `green` merges a second unvalidated change onto an
#     already-broken tree and buries the culprit — near-unrecoverable. Waiting
#     one wake costs nothing. So doubt ⇒ hold.
#   * review-quota: a false `exhausted` holds a lane whose sync was the correct
#     move, and holds it for DAYS (a seven-day window), fleet-wide, with no
#     self-heal and no un-wedge path short of a human — and the trigger for a
#     false `exhausted` (a GitHub/Anthropic payload format change) would be
#     correlated across every lane at once. A false `available` costs one wasted
#     sync, which is exactly what the loop already does today. So doubt ⇒ proceed.
#
# The issue's own acceptance criterion settles it: "Fails closed: if
# reviewability cannot be determined, behave as today (sync), since merging stale
# is the worse error." A future reader who "harmonises" the two polarities either
# re-introduces #1160 or wedges the fleet; test_review_quota.sh's
# `never_exhausted()` sweep and test_pr_ready.sh's inverted sweep pin both
# directions.
#
# ---------------------------------------------------------------------------
# `exhausted` IS A CONJUNCTION OF THREE POSITIVE PROOFS
# ---------------------------------------------------------------------------
#   (a) the newest CONCLUSIVE code-review.yml run in the window FAILED, AND
#   (b) its log carries a `rate_limit_event` whose `rate_limit_info.status` is
#       exactly `"rejected"` (case-SENSITIVE, leading double-quote included), AND
#   (c) THAT SAME BLOCK's `resetsAt` parses as an integer epoch strictly in the
#       future.
# Any link missing ⇒ not `exhausted`.
#
# ---------------------------------------------------------------------------
# THE DISCRIMINATOR: WHY THE LEADING `"` AND THE CASE ARE LOAD-BEARING
# ---------------------------------------------------------------------------
# This is the single most important line in the file. The Aug-7 re-run of PR
# #1158's review job (job 92768878061) concluded SUCCESS and posted a full LGTM
# review — and its log still contained:
#
#     "status": "allowed",
#     "resetsAt": 1786081200,
#     "rateLimitType": "five_hour",
#     "overageStatus": "rejected",
#     "overageDisabledReason": "out_of_credits",
#
# `overageStatus` / `out_of_credits` describe the overage BUDGET, not the
# request: `status` was `allowed` and the review ran to completion. So a bare
# `rejected` grep, an `overageStatus` grep, an `out_of_credits` grep, or ANY
# `grep -i` on `status` would each declare a perfectly healthy reviewer exhausted
# and hold every behind lane in the fleet for days, on a reviewer that was
# working the whole time, with no signal anywhere that anything was wrong.
# `overageStatus` differs from `status` by a capital S and a missing opening
# quote, so the case-SENSITIVE, quote-anchored `"status"` below does not match it
# and `grep -i` or a quote-less pattern would. Do not "simplify" either.
#
# ---------------------------------------------------------------------------
# SAME-BLOCK SCOPING: "the last resetsAt in the log wins" IS WRONG
# ---------------------------------------------------------------------------
# PR #1117's run log (30685290898) holds TWO rate_limit_event blocks: an early
# `"status": "allowed_warning"` at `utilization: 0.99`, then a real rejection
# ~2.5 minutes later — with DIFFERENT `resetsAt` values and possibly different
# `rateLimitType`s. So `resetsAt` and `rateLimitType` are read from a BOUNDED
# forward scan starting AT the matched status line (compact one-line JSON puts
# the whole event on that line) and stopping at the next `"status"` key or at the
# object's closing brace — never from a whole-log sweep. Borrowing a neighbour's
# window would hold a lane on an event that never rejected anything.
#
# Log lines arrive PREFIXED — `<job>\t<step>\t<timestamp> ` from
# `gh run view --log`, a bare timestamp from the raw job-log API — so every
# pattern here is unanchored at the start and tolerates leading noise, and the
# whitespace around each `:` is optional (`"status":"rejected"` and
# `"status" :  "rejected"` are the same JSON).
#
# ---------------------------------------------------------------------------
# HARDENING: THE LOG IS PARTLY ATTACKER-INFLUENCED
# ---------------------------------------------------------------------------
# `code-review.yml` runs with `show_full_output: true`, so the reviewing agent's
# output — derived from the PR diff and body — lands in the very log scanned
# here. A PR author can therefore plant the literal text `"status": "rejected"`
# with a future `resetsAt`. The worst case is a HOLD (throughput denial), never
# an unsafe merge, and `fleet.sh adopt` refuses cross-repository PRs — but the
# bar is raised anyway.
#
# WHAT ACTUALLY CARRIES WEIGHT, heaviest first — because the cheapest check is
# also the weakest one and must not be mistaken for the bar:
#   * the `success` SHORT-CIRCUIT. A healthy newest conclusive run never opens
#     its log at all, so on the common path there is nothing to plant text into.
#   * THE FAILED RUN IS NOT THE ATTACKER'S TO SCHEDULE. The newest conclusive
#     `code-review.yml` run REPO-WIDE — across every lane, not just theirs — must
#     already have FAILED before a single log line is read. A forger has to cause
#     that failure too and win the race against every other lane's review.
#   * FIELD VALIDATION. `rateLimitType` must be one of the known values and
#     `resetsAt` must be a PLAUSIBLE window (see RESET_HORIZON_SECONDS), so a
#     fabricated block answers `unknown` — today's `behind` → sync — rather than
#     holding the lane.
#   * NO log content ever reaches `eval`, a command substitution, an arithmetic
#     context, an unquoted expansion, or a `printf` FORMAT position — values are
#     extracted via bash's own `BASH_REMATCH` rather than by handing text to
#     another program, validated against a regex BEFORE any `(( ))` comparison,
#     and scrubbed through `safe_text` before any `warn`.
# The `"type": "rate_limit_event"` corroboration within TYPE_LOOKBACK lines is
# deliberately NOT on that list. Somebody who can plant one line can plant five,
# so it is anti-ACCIDENT, not anti-ATTACKER: it is what stops a PR that discusses
# rate limits, a doc quoting a payload, or an issue body pasted into a review
# from reading as a rejection. That is a real class of false positive — just not
# an adversary, and calling it the anti-forgery bar would be flattering it.
#
# RESIDUAL: an author who reproduces a full, well-formed rate_limit_event with a
# known `rateLimitType` and a plausible future `resetsAt`, on a run that has
# independently failed, can still hold their OWN lane (and any other behind lane
# that reads the same newest failed run) until the fabricated window elapses.
# That elapsing is what makes the residual acceptable, and it is only guaranteed
# by RESET_HORIZON_SECONDS: `EPOCH_RE` alone would accept a year-5138 `resetsAt`,
# i.e. an indefinite fleet-wide hold that never self-heals at all. Bounded, it is
# a denial of throughput on a repo whose PRs are already trusted enough to be
# adopted, for at most the horizon; it is accepted.
#
# STANDING RULE FOR THIS REPO: never commit a literal future `resetsAt` inside a
# literal `"status": "rejected"` + `"type": "rate_limit_event"` block, in ANY
# file — script, fixture, doc or issue body. Such a file becomes a self-forgery
# payload the moment a reviewing agent quotes that diff into a failing run's log:
# the repo would attack itself, with no attacker anywhere. Write the epoch as a
# shell VARIABLE (the committed bytes are then not an epoch at all) or as a
# literal in the PAST — time only moves forward, so a past literal can never
# expire INTO the future, the one direction that would flip a verdict.
# test_review_quota.sh greps ITSELF for this rule.
#
# ---------------------------------------------------------------------------
# CIRCUIT BREAKER, NOT BARRIER: the newest CONCLUSIVE run, not the newest run
# ---------------------------------------------------------------------------
# Reviews take minutes and every push to any lane starts another, so on a busy
# fleet the newest code-review.yml run is almost always still in flight. More:
# `skipped` is the NORMAL entry for a run Dependabot triggered (the job's `if:`
# evaluates false), and `cancelled` is the NORMAL entry for a review superseded
# by the workflow's own `concurrency` group. None of `skipped`, `cancelled`,
# `neutral`, `stale`, `action_required` or an empty conclusion says anything
# about whether the reviewer can review, so the walk SKIPS them and keeps going,
# in both directions — an inconclusive run above a success still reads
# `available`.
#
# ---------------------------------------------------------------------------
# SCOPE: `code-review.yml`, and NO `--branch` — the harmonisation trap
# ---------------------------------------------------------------------------
# main-health.sh's list call carries `--branch main`, because the run it reads is
# `ci.yml` on `push: main`. `code-review.yml` only ever triggers on
# `pull_request` (pinned by test_review_quota.sh against the workflow file), so a
# `--branch main` copied across from the sibling would yield an EMPTY window
# forever: this helper would answer `unknown` on every lane, always — and because
# `unknown` falls through to today's `behind` by design, nothing would ever look
# broken and #1160's bug would simply come back invisibly. Do not add it.
#
# ---------------------------------------------------------------------------
# COST BUDGET: 1 gh call, or exactly 2. NEVER 3, and NEVER a retry.
# ---------------------------------------------------------------------------
# Call 1 is the run list, always. Call 2 is `gh run view <id> --log`, made ONLY
# when the newest conclusive run failed (`code-review.yml` declares exactly one
# job, so the run log IS that job's log — no extra job-listing call). A newest
# conclusive `success` short-circuits with NO log read at all: that is both the
# cheap path and the second layer of defence against the false positive above,
# since the healthy-looking rejection text can then never even be reached.
# pr-ready.sh calls this per HELD lane per wake across the whole fleet, and the
# question being asked is literally "have we run out of API budget?" — a retry
# loop here is a rate limit there. The same arithmetic applies to what call 2
# RETURNS, which is unbounded: watch-pr.sh re-runs pr-ready.sh every 30s while a
# lane is held, so one held lane re-downloads and re-scans that log dozens of
# times. Hence LOG_TAIL_LINES — see that constant for the cost and the direction.
#
# Usage:  review-quota.sh [--repo <owner/repo>]
set -euo pipefail

# The workflow that IS the reviewer. See the SCOPE note above before touching
# either this or the deliberate absence of `--branch`.
readonly REVIEW_WORKFLOW="code-review.yml"

# How many runs back to look. Big enough that a cluster of skipped (Dependabot)
# and cancelled (superseded) reviews cannot hide the last conclusive answer,
# small enough to stay one cheap request. Same size as main-health.sh's, for the
# same reason.
readonly RUN_WINDOW=20

# One `gh run list` call, extracted server-side into one line per run, NEWEST
# FIRST, four `|`-separated fields: status|conclusion|databaseId|url. No headSha:
# a review run's blame range is not a thing. `// ""` on every field because
# `conclusion` is JSON null while a run is in flight, and `| tostring` on
# `databaseId` because it is a NUMBER — without it `join("|")` errors, jq exits
# non-zero, gh reports a failure, and this helper would answer `unknown` on every
# lane forever (silently, because `unknown` is the fall-through).
readonly RUN_FIELDS="status,conclusion,databaseId,url"
readonly RUN_JQ='.[] | [(.status // ""), (.conclusion // ""), ((.databaseId // "") | tostring), (.url // "")] | join("|")'

# The one status that means a run has something to say. Everything else is either
# in flight (below) or not evidence.
readonly STATUS_COMPLETED="completed"

# Statuses that mean "evidence is coming". GitHub reports `waiting`, `requested`
# and `pending` for approval- and deployment-gated runs alongside the two common
# ones; all of them are a run that has not concluded yet.
readonly -a IN_FLIGHT_STATUSES=(queued in_progress waiting requested pending)

# The conclusions that ARE evidence. `timed_out` and `startup_failure` count as
# failures because such a run still has a log worth reading — the #1158 rejection
# landed as a plain `failure` only because the action gave up in 24 seconds
# rather than hanging. Every other conclusion is not evidence (see the
# circuit-breaker note above).
readonly SUCCESS_CONCLUSION="success"
readonly -a FAILING_CONCLUSIONS=(failure timed_out startup_failure)

readonly TOKEN_AVAILABLE="available"
readonly TOKEN_EXHAUSTED="exhausted"
readonly TOKEN_UNKNOWN="unknown"

# THE DISCRIMINATOR. Case-SENSITIVE, opening quote included — see the block in
# the header for the live payload that proves both are load-bearing.
readonly REJECTED_RE='"status"[[:space:]]*:[[:space:]]*"rejected"'
# The corroborating line a real rejection sits inside (hardening proof, above).
readonly EVENT_TYPE_RE='"type"[[:space:]]*:[[:space:]]*"rate_limit_event"'
# Any `"status"` key at all — the boundary of the block being read. Note this
# CANNOT match `"overageStatus"`, for exactly the reason the discriminator does
# not: there is no `"` immediately before `status` there.
readonly STATUS_KEY_RE='"status"[[:space:]]*:'
# A closing brace at end of line, with or without a trailing comma — the other
# boundary. Deliberately NOT anchored at the start: `gh run view --log` prefixes
# every line with `<job>\t<step>\t<timestamp> `, so `^[[:space:]]*}` would never
# match a real log.
readonly OBJECT_CLOSE_RE='[}][[:space:]]*,?[[:space:]]*$'

# The shape BOTH sides of the reset-time comparison must have before either is
# allowed anywhere near an arithmetic context: a plain integer of at most 11
# digits. That covers every epoch until the year 5138 and rules out the
# overflow, negative and non-numeric shapes a log can carry — `"soon"`, `-1`,
# a 23-digit integer, an empty value. The bound is the point: this is the one
# value in the file that comes from attacker-influenceable text and is then
# USED as a number.
readonly EPOCH_RE='^[0-9]{1,11}$'

# …and the SEMANTIC bound the shape check above cannot express. Every
# epoch up to the year 5138 satisfies EPOCH_RE, and a rejection dated there would
# hold every behind lane in the fleet for longer than anyone reading this will be
# alive: an indefinite hold that never self-heals, which is precisely the residual
# risk the header refuses to accept. The reviewer's real windows are `five_hour`
# and `seven_day`, so nothing legitimate resets more than a week out; eight days
# is that week plus a day of slack for clock skew and for a longer window we have
# not observed yet. Past the horizon the block is not a rate-limit event we
# recognise, and — exactly like an unrecognised `rateLimitType` — it answers
# `unknown` ⇒ today's `behind` → sync. The asymmetry settles the size: a horizon
# set too TIGHT costs one wasted sync (what the loop already does today), while
# one set too WIDE costs the whole fleet its only remedy, for as long as the
# forged epoch says.
readonly RESET_HORIZON_DAYS=8
readonly RESET_HORIZON_SECONDS=$((RESET_HORIZON_DAYS * 86400))

# The shape a `databaseId` must have before it becomes an ARGUMENT to the second
# gh call. It is always quoted, so there is no injection here — the point is that
# a value which is not a plain integer means jq failed or the fields shifted
# under us, and spending a request to be told so is the one cost this file is
# most careful about. Same field-shape discipline pr-ready.sh applies to
# `behind_by` (`^[0-9]+$`) and `merge_base` (`^[0-9a-f]{7,40}$`).
readonly RUN_ID_RE='^[0-9]+$'

# How much of a log-derived value may reach stderr. `seven_day_opus` is fourteen
# characters and any genuinely new upstream enum will be short, so this is
# generous for recognising a real value and far too small to carry a planted
# paragraph. See safe_text() for why a bound is needed at all.
readonly SAFE_TEXT_MAX=80

# How far ABOVE a candidate rejection to look for its `"type":
# "rate_limit_event"` line, and how far BELOW it to look for that block's own
# fields. Four is enough for the pretty-printed shape (`{`, `"type"`,
# `"rate_limit_info": {`, then `"status"`), and seven forward reaches every field
# of the real block while stopping well short of the NEXT event — a scan wide
# enough to reach the next event would borrow its `resetsAt` and hold a lane on a
# window that never rejected anything. Both scans also include the matched line
# itself, because compact single-line JSON (what the raw job-log API returns)
# puts the whole event on it.
readonly TYPE_LOOKBACK=4
readonly FIELD_SCAN_LINES=7

# How much of the failed run's log to look at: the LAST this-many lines, never
# the whole thing. Two reasons, and the second is the one that bites.
#   COST. The block scoping needs random access, so the log is read into a bash
#   array — and bash's O(1) sequential-index cache only arrived in 4.3. Under the
#   house target (stock macOS /bin/bash 3.2) every `${arr[i]}` walks the list
#   from the head, so a whole-log scan is QUADRATIC. Measured on 3.2: 11,121
#   lines 0.54s, 55,605 lines 5.5s, 111,210 lines 17.8s — on an input nothing
#   bounds. Multiply by watch-pr.sh, which keeps `review-quota-exhausted` in its
#   in-flight set and therefore re-runs pr-ready.sh every 30s for up to half an
#   hour: ~60 full log downloads and re-scans per held lane, on a helper whose
#   entire purpose is to report that the API budget is gone.
#   CORRECTNESS. A real rejection is what the run DIES on, so it is always near
#   the end. Measured on the two real incidents: 914 lines from the end of PR
#   #1117's log, 175 from the end of PR #1158's. 2000 is more than twice the
#   worse of them.
# The tail is also the only end a bound may safely cut from, which is why one is
# allowed here at all: a truncated scan can only ever MISS a rejection — including
# the corner where the cut falls between a block's `"type"` line and its
# `"status"` line — and a missed rejection reads `available` ⇒ sync ⇒ exactly
# today's behaviour. It cannot invent one. So the bound can forgo a hold but can
# never create a false one, the same fail-safe direction as every other doubt
# path in this file.
readonly LOG_TAIL_LINES=2000

# The `rateLimitType` values we are willing to act on. An unrecognised value
# answers `unknown`, which falls through to today's `behind` → sync — so this
# list being too NARROW costs one wasted sync (today's behaviour), while being
# too WIDE is the injection surface described in the header. Widen it only for a
# value observed in a real log.
readonly -a RATE_LIMIT_TYPES=(five_hour seven_day seven_day_opus)

die()  { echo "review-quota: $1" >&2; exit 2; }
warn() { echo "review-quota: $1" >&2; }

# True when <needle> is one of the remaining arguments. Written as an explicit
# `if` rather than `[[ … ]] && return 0` so it carries no `set -e` hazard of its
# own, whatever context a future caller invokes it from.
in_list() { # in_list <needle> <candidate…>
  local needle="$1" item
  shift
  for item in "$@"; do
    if [[ "$needle" == "$item" ]]; then return 0; fi
  done
  return 1
}

# Does <text> match <ere>? The pattern is expanded UNQUOTED on purpose — a quoted
# right-hand side of `=~` is matched literally on bash 3.2 (stock /bin/bash on
# macOS), which would silently turn every pattern in this file into a plain
# substring search. The `if`/`return` shape keeps it free of `set -e` hazards.
matches() { # matches <text> <ere>
  if [[ "$1" =~ $2 ]]; then return 0; fi
  return 1
}

# The scalar value of a JSON key on one line, read with bash's OWN regex engine.
# No `sed`/`awk`/`eval` and no command built from log text: the log is partly
# attacker-influenced (see the hardening block), so it is only ever matched
# against, never executed or interpolated into anything that runs. Returns the
# empty string when the key is absent; the caller validates the shape.
field_value() { # field_value <key> <line>
  local re='"'"$1"'"[[:space:]]*:[[:space:]]*"?([^",}[:space:]]+)'
  if [[ "$2" =~ $re ]]; then printf '%s' "${BASH_REMATCH[1]}"; fi
}

# The only form in which log-derived text is allowed to reach stderr: scrubbed to
# a printable allowlist and capped at SAFE_TEXT_MAX.
#
# `field_value`'s character class excludes quotes, commas, braces and whitespace
# — but ESC (0x1B) is not `[:space:]`, no control character is, and the class is
# unbounded in length. So without this, a value we have ALREADY decided we will
# not act on could still carry a terminal-escape sequence, an embedded newline
# forging a second `review-quota:` line, or a paragraph of instructions. And this
# stderr is not swallowed — pr-ready.sh deliberately lets it through, because it
# is where the operator learns the quota is gone — so it lands both in a human's
# terminal (escape injection) and in the orchestrating agent's transcript (prompt
# injection). Reporting the value is worth keeping; reporting it verbatim is not.
#
# Allowlist, never a denylist, and pure parameter expansion — no external command
# is ever handed log text, for the same reason nothing else in this file is.
# Anything outside the set (including every byte of a multibyte character)
# becomes `?`, which is exactly what a diagnostic needs: enough to recognise an
# unfamiliar-but-real value, never enough to be a payload. The set carries no `|`
# on purpose: `|` is a shell metacharacter and is tokenised as `case` alternation
# BEFORE the bracket expression is read, so `[…|…]` would silently split this
# pattern in two and scrub half the allowlist along with it.
safe_text() { # safe_text <value>
  local raw="$1" out="" ch i limit="${#1}"
  if [[ "$limit" -gt "$SAFE_TEXT_MAX" ]]; then limit="$SAFE_TEXT_MAX"; fi
  for ((i = 0; i < limit; i++)); do
    ch="${raw:i:1}"
    case "$ch" in
      [0-9A-Za-z_.:/@=+,-] | ' ') out="$out$ch" ;;
      *) out="$out?" ;;
    esac
  done
  printf '%s' "$out"
}

# The UTC calendar day of an epoch, portably: BSD `date -r`, GNU `date -d @`.
# Never allowed to abort the script — a `date` that speaks neither dialect costs
# a nicety in the attribution, not a verdict.
utc_day() { # utc_day <epoch>
  date -u -r "$1" '+%Y-%m-%d' 2>/dev/null ||
    date -u -d "@$1" '+%Y-%m-%d' 2>/dev/null ||
    printf ''
}

# --- arguments: one optional flag, and NO positional arguments --------------
# This script asks about the REVIEWER, not about a PR. A stray number is a caller
# confusing it with pr-ready.sh, and silently ignoring it would answer a question
# nobody asked.
repo_args=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo) [[ $# -ge 2 ]] || die "--repo needs a value"; repo_args+=(--repo "$2"); shift 2 ;;
    -*)     die "unknown option: $1" ;;
    *)      die "unexpected argument: '$1' (usage: review-quota.sh [--repo <owner/repo>])" ;;
  esac
done

# --- call 1: the run list ---------------------------------------------------
# `${arr[@]+"${arr[@]}"}` expands to nothing when the array is empty instead of
# tripping `set -u` on bash 3.2, exactly as pr-ready.sh and main-health.sh do at
# their own `repo_args` expansions. No `--repo` is invented when none was given:
# gh then resolves the repo from the cwd, which is what a direct invocation wants.
#
# The assignment sits in an `if` condition so `set -e` cannot kill us before we
# print a token — and so gh printing a happy answer AND exiting non-zero (the
# dangerous shape: the output is already on stdout) still fails closed.
runs=""
if ! runs="$(gh run list --workflow "$REVIEW_WORKFLOW" \
    --limit "$RUN_WINDOW" --json "$RUN_FIELDS" --jq "$RUN_JQ" \
    ${repo_args[@]+"${repo_args[@]}"} 2>/dev/null)"; then
  warn "could not list $REVIEW_WORKFLOW runs (gh exited non-zero); not claiming the reviewer is out of quota"
  printf '%s\n' "$TOKEN_UNKNOWN"
  exit 0
fi

if [[ -z "$runs" ]]; then
  warn "no $REVIEW_WORKFLOW runs in the last $RUN_WINDOW"
  printf '%s\n' "$TOKEN_UNKNOWN"
  exit 0
fi

# --- the walk: newest first, stop at the first CONCLUSIVE run ---------------
verdict=""      # what the newest conclusive run decided, once we find it
failed_id=""    # attribution for the failed run whose log we then read
failed_url=""

while IFS= read -r line; do
  # Blank lines carry no run (the herestring always appends one at the end).
  [[ -n "$line" ]] || continue

  IFS='|' read -r status conclusion run_id url rest <<<"$line"

  # Split by FIELD COUNT, never by seeking a separator — the same rule and the
  # same reason as main-health.sh:248-280 and pr-ready.sh:323 / :370 / :483. A
  # status enum, a conclusion enum, an integer id and a URL can none of them
  # contain a `|`, so a surplus 5th field means a `|` appeared where none
  # legitimately can and the fields may have shifted under us; a missing one
  # means the payload is not the shape we asked for. `conclusion` is exempt: it
  # is legitimately empty while a run is in flight.
  #
  # `run_id` is held to its SHAPE and not merely to being non-empty, because it
  # is the one field here that goes on to become an ARGUMENT to another command.
  # A `databaseId` that is not a plain integer means the same thing a surplus
  # field does — jq failed, or the columns shifted — and catching it here spends
  # nothing, whereas passing it to `gh run view` spends a request from the very
  # budget this helper exists to measure. Checked for EVERY line, including ones
  # whose id is never used: a payload that is malformed anywhere is not one to
  # read a verdict out of.
  #
  # A malformed line STOPS THE WALK, and the stop resolves to `unknown` — full
  # stop. This is the ONE place this file is SIMPLER than main-health.sh, and the
  # simplification is deliberate: there, a malformed line below an already-proven
  # `red` keeps the red, because that walk continues past its verdict hunting a
  # blame-range floor. Here the walk stops AT the newest conclusive run and there
  # is nothing below it to hunt, so there is never a proven verdict for a
  # malformed line to survive. Do not port the more complex rule; it would have
  # nothing to preserve.
  if [[ -n "$rest" || -z "$status" || -z "$url" ]] ||
    ! matches "$run_id" "$RUN_ID_RE"; then
    warn "unusable run-list line ('$line'); refusing to classify the reviewer from a payload we cannot fully parse"
    printf '%s\n' "$TOKEN_UNKNOWN"
    exit 0
  fi

  if in_list "$status" "${IN_FLIGHT_STATUSES[@]}"; then continue; fi

  # Anything that is neither in flight nor completed is a status we do not
  # recognise — GitHub adds values over time. Treat it as no evidence and keep
  # walking rather than aborting: with no conclusive run behind it the walk still
  # ends on `unknown` anyway.
  [[ "$status" == "$STATUS_COMPLETED" ]] || continue

  if [[ "$conclusion" == "$SUCCESS_CONCLUSION" ]]; then
    verdict="$SUCCESS_CONCLUSION"
    break
  fi

  if in_list "$conclusion" "${FAILING_CONCLUSIONS[@]}"; then
    verdict="$conclusion"
    failed_id="$run_id"
    failed_url="$url"
    break
  fi

  # cancelled / skipped / neutral / action_required / stale / empty: not
  # evidence about whether the reviewer can review. Skip and keep walking.
done <<<"$runs"

if [[ -z "$verdict" ]]; then
  warn "the last $RUN_WINDOW $REVIEW_WORKFLOW runs carry no conclusive result (all skipped, cancelled or still in flight)"
  printf '%s\n' "$TOKEN_UNKNOWN"
  exit 0
fi

if [[ "$verdict" == "$SUCCESS_CONCLUSION" ]]; then
  # DEFENCE LAYER TWO, and the cheap path: a success is positive proof the
  # reviewer reviewed, so there is nothing its log could add — and the log of a
  # HEALTHY reviewer can itself look like a rejection (see the discriminator
  # block). Never opened. The healthy path also says nothing on stderr.
  printf '%s\n' "$TOKEN_AVAILABLE"
  exit 0
fi

# --- call 2: the failed run's log -------------------------------------------
# `code-review.yml` declares exactly one job, so the RUN log IS that job's log —
# no extra job-listing call. Same `if`-condition guard as call 1, for the same
# two reasons.
log=""
if ! log="$(gh run view "$failed_id" --log ${repo_args[@]+"${repo_args[@]}"} 2>/dev/null)"; then
  warn "could not read the log of failed $REVIEW_WORKFLOW run $failed_id ($failed_url); not claiming the reviewer is out of quota"
  printf '%s\n' "$TOKEN_UNKNOWN"
  exit 0
fi

if [[ -z "$log" ]]; then
  warn "the log of failed $REVIEW_WORKFLOW run $failed_id ($failed_url) came back empty"
  printf '%s\n' "$TOKEN_UNKNOWN"
  exit 0
fi

# Bound the scan BEFORE anything reads the log — see LOG_TAIL_LINES for why the
# tail, and why cutting there can only ever forgo a hold.
#
# A SEPARATE STEP, never `gh … | tail` on the capture above. `set -o pipefail`
# would still surface a failing gh, but the `if !` capture guard is what catches
# the dangerous shape — gh printing a plausible answer AND exiting non-zero — and
# that guard has to stay alone on the call, with nothing downstream able to
# rewrite what it saw.
if ! log="$(tail -n "$LOG_TAIL_LINES" <<<"$log")"; then
  warn "could not bound the log of failed $REVIEW_WORKFLOW run $failed_id ($failed_url) to its last $LOG_TAIL_LINES lines; not claiming the reviewer is out of quota"
  printf '%s\n' "$TOKEN_UNKNOWN"
  exit 0
fi

# Read the log once into an addressable form: the block scoping needs to look
# both above and below the matched line, and a single forward stream cannot.
# `line_count` is tracked by hand rather than read from `${#log_lines[@]}`, which
# is an unbound-variable error for an empty array under `set -u` on bash 3.2.
log_lines=()
line_count=0
while IFS= read -r log_line; do
  log_lines+=("$log_line")
  line_count=$((line_count + 1))
done <<<"$log"

# The NEWEST credible rejection wins. The log is chronological, so the last
# rate-limit event describes the reviewer's current state; an earlier one may
# already have been superseded (the #1117 log's `allowed_warning` then rejection
# is exactly that shape). Choosing the newest is also the better answer under the
# injection concern: a planted line lands in the agent's mid-review output, while
# a real rejection is what the run dies on.
#
# CREDIBLE means the corroborating `"type": "rate_limit_event"` sits within
# TYPE_LOOKBACK lines above it. A bare planted `"status": "rejected"` is
# therefore not a candidate at all, and a log carrying only such lines reads
# `available` — the same answer as a log with no rejection in it, which is what
# it is.
rej_idx=-1
for ((i = 0; i < line_count; i++)); do
  if ! matches "${log_lines[i]}" "$REJECTED_RE"; then continue; fi
  for ((j = i; j >= 0 && j > i - TYPE_LOOKBACK - 1; j--)); do
    if matches "${log_lines[j]}" "$EVENT_TYPE_RE"; then rej_idx=$i; break; fi
  done
done

if [[ "$rej_idx" -lt 0 ]]; then
  # The run failed for an ordinary reason — a bad diff, a transient 5xx, a broken
  # action, an empty structured output. That says nothing about the quota, and it
  # must not. Silent, like every other `available`.
  printf '%s\n' "$TOKEN_AVAILABLE"
  exit 0
fi

# --- that block's own fields, and nobody else's -----------------------------
reset_raw=""
limit_type=""
for ((i = rej_idx; i < line_count && i <= rej_idx + FIELD_SCAN_LINES; i++)); do
  scan_line="${log_lines[i]}"
  if [[ "$i" -gt "$rej_idx" ]]; then
    # The next event's `"status"` key, or this object's closing brace, ends the
    # block. Past either, any `resetsAt` belongs to a different event and a
    # different limit — borrowing it is what makes "last resetsAt in the log
    # wins" wrong (see the same-block scoping note in the header).
    if matches "$scan_line" "$STATUS_KEY_RE"; then break; fi
    if matches "$scan_line" "$OBJECT_CLOSE_RE"; then break; fi
  fi
  if [[ -z "$reset_raw" ]]; then reset_raw="$(field_value resetsAt "$scan_line")"; fi
  if [[ -z "$limit_type" ]]; then limit_type="$(field_value rateLimitType "$scan_line")"; fi
done

# Proof (c) is missing: a rejection we cannot date is a rejection we cannot say
# is still in force. `unknown`, never `exhausted`.
#
# The rejected value is REPORTED — an operator debugging a helper that suddenly
# answers `unknown` on every lane needs to see what the payload actually said —
# but only through safe_text: this is a value that failed validation, which is
# the exact population most likely to be hostile.
if ! matches "$reset_raw" "$EPOCH_RE"; then
  reset_shown="$(safe_text "$reset_raw")"
  warn "failed $REVIEW_WORKFLOW run $failed_id ($failed_url) carries a rate-limit rejection with no usable resetsAt (got '${reset_shown:-<none>}'); cannot tell whether the window is still open"
  printf '%s\n' "$TOKEN_UNKNOWN"
  exit 0
fi

# The hardening check. An invented `rateLimitType` is the cheapest tell of a
# fabricated block, and a genuinely NEW one upstream is a payload we have not
# read before — both answer `unknown`, i.e. today's behaviour. Reported through
# safe_text for the same reason as the resetsAt path above: the value that
# reaches this branch is by definition one we just refused to trust, and a
# `warn` is a straight pipe from the log to a terminal and to an agent's context.
if ! in_list "$limit_type" "${RATE_LIMIT_TYPES[@]}"; then
  limit_shown="$(safe_text "$limit_type")"
  warn "failed $REVIEW_WORKFLOW run $failed_id ($failed_url) carries a rate-limit rejection with an unrecognised rateLimitType ('${limit_shown:-<none>}'); refusing to hold a lane on a block we cannot validate"
  printf '%s\n' "$TOKEN_UNKNOWN"
  exit 0
fi

# Held to the SAME shape as the value read from the log, and for the same reason:
# nothing enters the comparison below until it is proven to be plain digits.
now="$(date -u +%s 2>/dev/null || true)"
if ! matches "$now" "$EPOCH_RE"; then
  warn "date(1) did not return a usable epoch ('${now:-<none>}'), so the reset time cannot be compared"
  printf '%s\n' "$TOKEN_UNKNOWN"
  exit 0
fi

# Both operands are already proven to be plain digit strings, so this arithmetic
# can only ever compare numbers — the validation above is what makes it safe to
# put a value read from the log into `(( ))` at all. `10#` forces base 10 so a
# leading zero cannot be read as octal and abort the shell.
if (( 10#$reset_raw <= 10#$now )); then
  # The window has already reset: the reviewer came back, and a lane held on this
  # would be held on nothing. Silent, like every other `available`.
  printf '%s\n' "$TOKEN_AVAILABLE"
  exit 0
fi

# The window is in the future — but "in the future" is not the same as "a real
# window", and this is the last place the difference can be caught. A `resetsAt`
# past the horizon is not a rate-limit event we recognise, so it is treated
# exactly like an unrecognised `rateLimitType`: `unknown` ⇒ today's `behind` →
# sync. Without it the residual risk the header accepts would be unbounded rather
# than self-healing — EPOCH_RE alone admits every epoch to the year 5138, so ONE
# forged line could hold every behind lane in the fleet for three thousand years,
# and a hold nobody alive will see lift cannot be excused as "it self-heals".
#
# Both operands were proven to be plain digit strings above, so this is still
# arithmetic on numbers only; `10#` forces base 10 for the same reason as above.
if (( 10#$reset_raw > 10#$now + RESET_HORIZON_SECONDS )); then
  warn "failed $REVIEW_WORKFLOW run $failed_id ($failed_url) carries a rate-limit rejection dated more than $RESET_HORIZON_DAYS days out (resetsAt $reset_raw); no real reviewer window is that long, so this is not a block we will hold a lane on"
  printf '%s\n' "$TOKEN_UNKNOWN"
  exit 0
fi

# All three proofs hold. Attribute loudly — this is the answer that stops the
# fleet's only remedy for a stale lane, and the operator needs to know which run
# proved it and when it lifts. Both the raw epoch (machine-checkable against the
# log) and a UTC day (human-readable) are reported.
reset_day="$(utc_day "$reset_raw")"
warn "reviewer is OUT OF QUOTA: $REVIEW_WORKFLOW run $failed_id concluded $verdict with a rate-limit rejection ($failed_url)"
warn "the $limit_type window resets at epoch $reset_raw${reset_day:+ (UTC $reset_day)}; until then a sync would destroy a lane's LGTM with no way to earn it back"
printf '%s\n' "$TOKEN_EXHAUSTED"
