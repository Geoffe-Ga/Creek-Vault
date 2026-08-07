#!/usr/bin/env bash
# scripts/ralph/watch-pr.sh
#
# Per-lane hot watch for LOCAL orchestrator sessions (ralph-tick.md Step 5).
# A remote/mobile session gets woken by `subscribe_pr_activity` webhooks the
# moment a lane's verdict or CI failure lands; a local terminal session has no
# webhook MCP at all, so before this script it slept the full ScheduleWakeup
# fallback (~20–30 min) past every one of those events. On BOTH platforms a
# background Bash task's exit re-invokes the session — so a process that exits
# exactly when a lane leaves its in-flight state IS a per-lane wake, and this
# script is that process: the orchestrator launches one per in-flight PR as a
# background task, ends the turn, and the first lane to settle wakes it.
#
# It polls `scripts/ralph/pr-ready.sh <PR>` (the single authoritative
# classifier — never a rollup grep) every INTERVAL seconds and exits the moment
# the token leaves the IN-FLIGHT set {pending, awaiting-review, main-not-green};
# every other token (ready, ready-unreviewed, behind, ci-failed,
# changes-requested, optout) is a state the orchestrator acts on, so it is worth
# a wake. Output is exactly one line:
#
#   WATCH <PR> already-watching     another live watcher owns this PR (pidfile)
#   WATCH <PR> <token>              the lane settled; <token> is pr-ready.sh's
#   WATCH <PR> gone                 the PR merged or closed while watching
#   WATCH <PR> timeout <last-token> TIMEOUT elapsed with the lane still in flight
#
# EVERY wait outcome exits 0 — like pr-ready.sh, a non-zero exit means a
# usage/tooling error at startup, never a verdict about the PR. In particular a
# transient pr-ready.sh failure (rate limit, 5xx, expired token — it exits 2
# with nothing on stdout) must NOT kill the watcher: dying there would convert
# GitHub weather into a spurious wake plus a lane nobody is watching, so the
# loop tolerates it, keeps the last good token, and polls again.
#
# IDEMPOTENT BY PIDFILE: the orchestrator (re)launches watchers for every
# in-flight PR on every wake without bookkeeping. A live pidfile at
# $RALPH_WATCH_PIDDIR/ralph-watch-<reposlug>-<PR>.pid (default /tmp) makes the
# duplicate exit 0 immediately with `already-watching` — a no-op wake — while a
# stale pidfile (dead pid, e.g. after a reboot) is simply taken over.
#
# Usage:  watch-pr.sh <PR_NUMBER> [INTERVAL=30] [TIMEOUT=1800]
set -euo pipefail

readonly DEFAULT_INTERVAL=30
readonly DEFAULT_TIMEOUT=1800

# pr-ready.sh's in-flight tokens — the ONLY three on which the lane is genuinely
# "wait for GitHub". Everything else it prints calls for orchestrator action.
#
# `main-not-green` (issue #1159) is the third: the lane is held because `main`'s
# own CI is red / still running / unreadable, and there is nothing the
# orchestrator can do about that — not merge (the backstop is dead), not sync
# (that imports the breakage). It is a wait, and `main` CI takes ~14 minutes per
# round. Leaving it OUT of this set would be worse than the bug #1159 fixes and
# much harder to see: the watcher would exit on its very first poll, the
# orchestrator would relaunch it (the pidfile is gone, so the idempotence guard
# does not catch it), it would exit immediately again, and the whole fleet would
# busy-wake at wake speed for as long as `main` stayed red — burning the API
# budget precisely when nobody can merge anything.
readonly -a IN_FLIGHT_TOKENS=(pending awaiting-review main-not-green)

# `gh pr view --json state` values that mean the PR no longer exists to watch.
readonly MERGED_STATE="MERGED"
readonly CLOSED_STATE="CLOSED"

die() { echo "watch-pr: $1" >&2; exit 2; }

pr="${1-}"
interval="${2:-$DEFAULT_INTERVAL}"
timeout="${3:-$DEFAULT_TIMEOUT}"
[[ "$pr" =~ ^[0-9]+$ ]] ||
  die "usage: watch-pr.sh <PR_NUMBER> [INTERVAL=$DEFAULT_INTERVAL] [TIMEOUT=$DEFAULT_TIMEOUT]"
# Fractional INTERVALs are legal (GNU sleep accepts them; the tests use them),
# but TIMEOUT stays an integer because the deadline math is whole epoch seconds.
[[ "$interval" =~ ^[0-9]+(\.[0-9]+)?$ ]] || die "INTERVAL must be a number, got '$interval'"
[[ "$timeout" =~ ^[0-9]+$ ]] || die "TIMEOUT must be an integer number of seconds, got '$timeout'"

READY="$(cd "$(dirname "$0")" && pwd)/pr-ready.sh"
[[ -x "$READY" ]] || die "cannot find pr-ready.sh next to this script ($READY)"

# One watcher per repo per PR. The slug comes from the origin URL so two clones
# of DIFFERENT repos never share a pidfile; a clone with no origin falls back to
# its directory name. Sanitized to [A-Za-z0-9_-] so the slug can never smuggle
# path separators into the pidfile path.
repo_slug() {
  local url slug
  url="$(git config --get remote.origin.url 2>/dev/null || true)"
  if [[ -n "$url" ]]; then
    slug="${url%/}"; slug="${slug%.git}"
    slug="$(basename "$slug")"
  else
    slug="$(basename "$(git rev-parse --show-toplevel 2>/dev/null || pwd)")"
  fi
  printf '%s\n' "$slug" | tr -c 'A-Za-z0-9_-' '-' | tr -s '-' | sed 's/-$//'
}

pidfile="${RALPH_WATCH_PIDDIR:-/tmp}/ralph-watch-$(repo_slug)-${pr}.pid"

if [[ -f "$pidfile" ]]; then
  old_pid="$(cat "$pidfile" 2>/dev/null || true)"
  if [[ "$old_pid" =~ ^[0-9]+$ ]] && kill -0 "$old_pid" 2>/dev/null; then
    echo "WATCH $pr already-watching"
    exit 0
  fi
  # Dead pid → stale file (a rebooted machine, a killed session). Take over.
fi
echo "$$" > "$pidfile"
trap 'rm -f "$pidfile"' EXIT

in_flight() { # in_flight <token> — is this a keep-waiting token?
  local t
  for t in "${IN_FLIGHT_TOKENS[@]}"; do
    [[ "$1" == "$t" ]] && return 0
  done
  return 1
}

start="$(date +%s)"
last_token="unknown"

while :; do
  # A merged/closed PR would sit in the poll forever (pr-ready.sh classifies
  # open work, not absence), so the terminal states are checked explicitly.
  # The lookup failing is the same transient weather as a pr-ready failure —
  # keep looping, never die.
  state="$(gh pr view "$pr" --json state --jq '.state' 2>/dev/null || true)"
  if [[ "$state" == "$MERGED_STATE" || "$state" == "$CLOSED_STATE" ]]; then
    echo "WATCH $pr gone"
    exit 0
  fi

  # `|| true` twice over: pr-ready.sh exits 2 on a tooling error (with nothing
  # on stdout), and under `set -e` an unguarded call would kill the watcher —
  # exactly the die-on-transient-failure this script exists to avoid.
  token="$(bash "$READY" "$pr" 2>/dev/null || true)"
  if [[ -n "$token" ]]; then
    last_token="$token"
    if ! in_flight "$token"; then
      echo "WATCH $pr $token"
      exit 0
    fi
  fi

  now="$(date +%s)"
  if (( now - start >= timeout )); then
    echo "WATCH $pr timeout $last_token"
    exit 0
  fi
  sleep "$interval"
done
