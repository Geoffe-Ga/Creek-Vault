#!/usr/bin/env bash
# scripts/ralph/pr-ready.sh
#
# Authoritative "is this lane safe to merge?" check for the Ralph orchestrator
# (ralph-tick.md Step 1). Prints exactly one status token and exits 0 (it is a
# query — a non-zero exit means a usage/tooling error, never a PR verdict):
#
#   ready            LGTM (fresh) + CI green + up-to-date with main  → merge now
#   behind           LGTM (fresh) + CI green but mergeStateStatus BEHIND → sync first
#   pending          CI still running → wait for a later wake
#   ci-failed        CI has a failing/errored check → Step 2 (ci-debugging)
#   awaiting-review  no fresh LGTM verdict (missing, stale, or non-LGTM) → wait / Step 2
#
# WHY THIS EXISTS: the previous all-lanes Monitor grepped `gh pr checks` output
# for ': pending'. That output is TAB-delimited (name<TAB>pending<TAB>...), so the
# grep never matched and a still-running CI was read as settled — a false READY
# that could merge a PR with pending/failing checks. CI state here is keyed off
# the `gh pr checks` EXIT CODE, which is authoritative: 0 = all passed, 8 = some
# pending, anything else = failure. No text parsing of the checks table at all.
#
# Stale-verdict guard: a review verdict only counts when it was posted AFTER the
# PR's HEAD commit. An LGTM from before the latest push is stale (it reviewed
# older code) and must not gate a merge.
#
# Usage:  pr-ready.sh <PR_NUMBER> [--repo <owner/repo>]
set -euo pipefail

# `gh pr checks` exit code that means "checks still pending" (gh's documented
# contract: 0 = pass, 8 = pending, other = failure).
readonly CHECKS_PENDING_EC=8

die() { echo "pr-ready: $1" >&2; exit 2; }

pr=""
repo_args=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo) [[ $# -ge 2 ]] || die "--repo needs a value"; repo_args+=(--repo "$2"); shift 2 ;;
    -*)     die "unknown option: $1" ;;
    *)      [[ -z "$pr" ]] || die "unexpected extra argument: $1"; pr="$1"; shift ;;
  esac
done
[[ "$pr" =~ ^[0-9]+$ ]] || die "usage: pr-ready.sh <PR_NUMBER> [--repo <owner/repo>]"

# The canonical verdict line `claude-code-review.yml` posts is
# `## Verdict: <LGTM|CHANGES_REQUESTED|COMMENTS>` (also tolerated: `**Verdict:**`
# and a bare `Verdict:`), sitting at the END of a longer `## Summary …` body — so
# the match must be case-insensitive AND multiline (`m`, so `^` anchors to the
# verdict line — which sits at the END of a multi-line `## Summary …` body, not
# at string start), prefix-tolerant, and keyed to the verdict LINE (a stray
# "LGTM" in prose must not count). This mirrors the canonical parser in
# `.claude/skills/await-claude-review/SKILL.md`. Backslashes are doubled because
# this text is spliced into a jq string literal, where `\s` is an invalid escape
# and must reach the regex engine as `\\s`.
readonly VERDICT_RE='(?im)^\\s*(?:#{1,6}\\s+|\\*\\*)?verdict[:*\\s]'
readonly VERDICT_LGTM_RE="${VERDICT_RE}+lgtm"

# `${arr[@]+"${arr[@]}"}` expands to nothing when the array is empty instead of
# tripping `set -u` on bash 3.2 (stock /bin/bash on macOS).
gh_args=("$pr" ${repo_args[@]+"${repo_args[@]}"})

# --- CI state from the exit code, not the text table -----------------------
ci_ec=0
gh pr checks "${gh_args[@]}" >/dev/null 2>&1 || ci_ec=$?
if [[ "$ci_ec" -eq "$CHECKS_PENDING_EC" ]]; then
  echo "pending"; exit 0
elif [[ "$ci_ec" -ne 0 ]]; then
  echo "ci-failed"; exit 0
fi

# --- CI is green: check mergeability + a FRESH LGTM verdict -----------------
# One call yields "<mergeStateStatus>|<HEAD committedDate>", another the latest
# top-level verdict as "<createdAt>|<isLGTM>". gh applies --jq server-side.
merge_line="$(gh pr view "${gh_args[@]}" \
  --json mergeStateStatus,commits \
  --jq '(.mergeStateStatus // "") + "|" + (.commits[-1].committedDate // "")')"
merge_state="${merge_line%%|*}"
head_date="${merge_line#*|}"

verdict_line="$(gh pr view "${gh_args[@]}" \
  --json comments \
  --jq "([.comments[] | select(.body != null and (.body | test(\"$VERDICT_RE\")))] | last) as \$v
        | ((\$v.createdAt // \"\") + \"|\" + ((\$v.body // \"\" | test(\"$VERDICT_LGTM_RE\")) | tostring))")"
verdict_date="${verdict_line%%|*}"
verdict_lgtm="${verdict_line#*|}"

# Without a HEAD commit time we cannot prove the verdict is fresh — fail closed.
if [[ -z "$head_date" ]]; then
  echo "awaiting-review"; exit 0
fi

# Fresh LGTM ⇔ latest verdict is LGTM AND its createdAt is strictly newer than
# the HEAD commit. RFC3339 UTC timestamps are fixed-width, so a lexical string
# compare is a correct chronological compare (portable — no date arithmetic).
if [[ "$verdict_lgtm" != "true" || -z "$verdict_date" ]] || ! [[ "$verdict_date" > "$head_date" ]]; then
  echo "awaiting-review"; exit 0
fi

if [[ "$merge_state" == "CLEAN" ]]; then
  echo "ready"
else
  echo "behind"
fi
