#!/usr/bin/env bash
# scripts/ralph/pr-ready.sh
#
# Authoritative "is this lane safe to merge?" check for the Ralph orchestrator
# (ralph-tick.md Step 1). Prints exactly one status token and exits 0 (it is a
# query — a non-zero exit means a usage/tooling error, never a PR verdict):
#
#   ready            LGTM (fresh) + CI green + verified current with main → merge now
#   ready-unreviewed CI green (with real checks that actually passed) + verified
#                    current with main, but this PR HAS no review gate: Dependabot
#                    authored it AND pushed its HEAD commit, and `claude-review`
#                    reported SKIPPED → the orchestrator decides (see ralph-tick.md)
#   behind           LGTM (fresh) + CI green but the branch is not current → sync first
#   pending          CI still running (or no checks registered yet) → wait for a later wake
#   ci-failed        CI has a failing/errored check → Step 2 (ci-debugging)
#   awaiting-review  no fresh LGTM verdict (missing, stale, or non-LGTM) → wait / Step 2
#   optout           `do-not-auto-merge` on the PR or on the issue it closes → the
#                    loop does not act on this PR AT ALL; a human owns it. Checked
#                    first, and an unreadable label answer exits 2 rather than
#                    assuming no hold.
#
# WHY AN UNDETERMINABLE HOLD IS A TOOLING ERROR: the opt-out check makes three
# lookups (the PR's labels, the PR's body, the linked issue's labels), and any of
# them can fail on a rate limit, a 5xx, or an expired token. Reading such a
# failure as "unlabelled" would let transient GitHub weather silently defeat the
# ONE control a human retains over an autonomous merge loop — the orchestrator
# would be handed `ready` for a PR somebody had explicitly parked. So a failed
# lookup `die`s (exit 2, nothing on stdout), which this script's contract already
# defines as a tooling error and never a verdict: the orchestrator acts on no
# lane it cannot classify, and the next wake retries.
#
# WHY THIS EXISTS: the previous all-lanes Monitor grepped `gh pr checks` output
# for ': pending'. That output is TAB-delimited (name<TAB>pending<TAB>...), so the
# grep never matched and a still-running CI was read as settled — a false READY
# that could merge a PR with pending/failing checks. CI state here is keyed off
# the `gh pr checks` EXIT CODE, which is authoritative: 0 = all passed, 8 = some
# pending, anything else = failure. No text parsing of the checks table at all —
# with one deliberate exception: a stderr *signature* match (not table parsing)
# for gh's no-checks-yet exit-1 case, which is reclassified from failure to pending.
#
# WHY `ready-unreviewed` EXISTS: `.github/workflows/code-review.yml` skips its
# `claude-review` job on runs Dependabot TRIGGERED (GitHub withholds Actions
# secrets — including the review token — from those runs, so the action would
# hard-fail before reviewing). A PR nobody but Dependabot has touched therefore
# never grows a verdict and could only ever print `awaiting-review`: a lane
# waiting for `ready` would hang forever on exactly the PRs the loop adopts in
# order to merge. Any commit WE push to a bump (a sync, a forward-adaptation)
# makes the review job runnable again and lands the lane back on the normal
# `ready` path, so this token covers only the residual case of a bump already
# green and already current, where nothing of ours is ever pushed. It is a
# SEPARATE token rather than a looser `ready` on purpose: `ready` keeps its full
# four-part meaning (fresh LGTM + green CI + CLEAN + `behind_by == 0`), so the
# decision about what to do with a PR no reviewer ever saw is made visibly in
# `ralph-tick.md` and never silently here — and in THIS repo ralph-tick.md's
# Step 1 routing does not merge on this token, it hands the lane to a human.
#
# Two conditions beyond "Dependabot authored it" make that safe, because the
# token's whole justification is "green CI against current main replaces the
# review":
#   * At least one NON-review check must have actually SUCCEEDED. `gh pr checks`
#     exits 0 when every check merely skipped, and the test workflows here are
#     `paths:`-filtered to their own sources — so a `github-actions` ecosystem
#     bump (which touches only `.github/workflows/*.yml`) matches none of them
#     and lands zero checks. Without this, "green" would mean "no CI ran at all"
#     on exactly the PRs that rewire the workflows holding our secrets.
#   * Dependabot must also have pushed the HEAD commit. `statusCheckRollup` is
#     per-HEAD-commit, so a bot force-push (a `@dependabot recreate`, a group
#     recomputation) landing after we adapted a branch would otherwise hand a
#     fresh all-SKIPPED rollup back to this token and re-clear hand-written —
#     possibly already-rejected — code as never-touched.
#
# Stale-verdict guard: a review verdict only counts when it was posted AFTER the
# PR's HEAD commit. An LGTM from before the latest push is stale (it reviewed
# older code) and must not gate a merge.
#
# Freshness guard: `mergeStateStatus` is NOT a freshness signal. GitHub computes
# BEHIND only when the base branch enforces strict/up-to-date status checks,
# which this repo does not — so a branch many commits behind `main` reports
# CLEAN, and its own green CI proves nothing about today's `main`. Measured
# here: PR #943 reports MERGEABLE/UNSTABLE while the compare API says
# `behind_by: 22`, and #863 says 44. #943 is a `ruff` bump, and the identical
# situation upstream (a bump 17 behind carrying a ruff major) produced 144 lint
# errors against the then-current tree — merging its stale green would have
# turned `main` red. Freshness therefore comes from the compare API's
# `behind_by`, and CLEAN is KEPT alongside it because DIRTY / CONFLICTING /
# BLOCKED / DRAFT / UNKNOWN are invisible to `behind_by`. The probe is LAZY by
# design — only a lane that would otherwise print `ready` pays for it, so the
# orchestrator never burns a request per lane per wake on already-decided lanes.
#
# Usage:  pr-ready.sh <PR_NUMBER> [--repo <owner/repo>]
set -euo pipefail

# `gh pr checks` exit code that means "checks still pending" (gh's documented
# contract: 0 = pass, 8 = pending, other = failure).
readonly CHECKS_PENDING_EC=8

# gh also exits 1 (a "failure") with this case-insensitive stderr substring when a
# PR has no check runs registered yet: `no checks reported on the '<branch>' branch`.
# That is a not-yet-started PR, not a failed one, so it must classify as `pending`.
# We never pass `--required`, so gh's `--required` variant ("no required checks
# reported...", which this substring would NOT match) never arises here.
readonly NO_CHECKS_SIG='no checks reported'

# The per-PR/per-issue human hold. Byte-for-byte the same string as the
# `do-not-auto-merge` entry in `pick-next.sh`'s EXCLUDE_LABELS default — that
# file already excludes a labelled issue at issue-PICK time, and this is the
# PR-side counterpart, so the two must never drift apart.
readonly OPTOUT_LABEL="do-not-auto-merge"

# The issue-link vocabulary the rest of the loop uses (pick-next.sh's in-flight
# scan, the Dependabot bridge). Case-insensitivity comes from `grep -i`.
readonly ISSUE_LINK_RE='(closes|fixes|resolves)[[:space:]]+#[0-9]+'

# Placeholder slug `gh` substitutes from the current repo when no --repo is given.
readonly CURRENT_REPO_SLUG='{owner}/{repo}'

# The one PR class whose review gate provably cannot exist. Dependabot spells its
# login differently PER FIELD, and both spellings are exact-match tightness
# guards on `ready-unreviewed` — without them, any future skip condition on the
# review workflow would start auto-merging unreviewed HUMAN PRs. All four values
# below were read off a live bump in THIS repo (PR #943): `--json author` →
# `app/dependabot`, `commits[-1].authors[0].login` → `dependabot[bot]`, the
# rollup's `claude-review` entry → `SKIPPED`, alongside 3 non-review `SUCCESS`
# checks.
readonly DEPENDABOT_AUTHOR="app/dependabot"
readonly DEPENDABOT_COMMIT_AUTHOR="dependabot[bot]"

# The `code-review.yml` job name as it appears in the status rollup (the job KEY;
# that job declares no `name:` override, so the check name matches it), and the
# conclusions GitHub reports for a job whose `if:` evaluated false and for one
# that genuinely passed.
readonly REVIEW_CHECK_NAME="claude-review"
readonly SKIPPED_CONCLUSION="SKIPPED"
readonly SUCCESS_CONCLUSION="SUCCESS"

# How many non-review checks must have actually passed before "CI is green" may
# stand in for a review. One is enough to prove CI ran at all, which is the whole
# claim; all-skipped is what this rules out.
readonly MIN_NON_REVIEW_SUCCESSES=1

die() { echo "pr-ready: $1" >&2; exit 2; }

pr=""
# The bare slug, kept alongside `repo_args` because `branch_is_current` builds a
# REST path (`repos/<slug>/compare/...`) rather than passing `--repo` to gh.
repo_slug=""
repo_args=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo) [[ $# -ge 2 ]] || die "--repo needs a value"; repo_args+=(--repo "$2"); repo_slug="$2"; shift 2 ;;
    -*)     die "unknown option: $1" ;;
    *)      [[ -z "$pr" ]] || die "unexpected extra argument: $1"; pr="$1"; shift ;;
  esac
done
[[ "$pr" =~ ^[0-9]+$ ]] || die "usage: pr-ready.sh <PR_NUMBER> [--repo <owner/repo>]"

# The canonical verdict line `code-review.yml` posts is
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

# --- opt-out, checked FIRST so a held PR is never even probed ---------------
# Whole-LINE match: `do-not-auto-merge-after-review` is a different label and must
# not read as a hold. Uses a herestring, never `printf … | grep -q`: that pipeline
# is a pipefail/SIGPIPE inversion (grep -q exits on first match, the writer dies
# 141, and the pipeline reports non-zero on a MATCH) — the same hazard
# .github/workflows/code-review.yml already documents at its `changed_files` grep.
has_optout_label() { grep -qxF "$OPTOUT_LABEL" <<<"$1"; }

# One object's labels, one per line. Non-zero on API failure so the caller can
# refuse to classify rather than infer "unlabelled".
labels_of() { # labels_of <pr|issue> <number>
  gh "$1" view "$2" ${repo_args[@]+"${repo_args[@]}"} --json labels --jq '.labels[].name'
}

# The LAST issue this PR's body closes, or empty when it links none. LAST, not
# first: a Dependabot body embeds the dependency's own changelog, whose
# "* Fixes #456" lines sit BEFORE the bridge's appended `Closes #N` — reading the
# first link would point the hold lookup at an unrelated upstream tracker's issue
# number and miss the hold on the bridge issue. `tr -dc '0-9'` also sanitizes the
# value before it is ever handed to `gh`.
linked_issue() {
  { grep -oiE "$ISSUE_LINK_RE" <<<"$1" || true; } | tail -n 1 | tr -dc '0-9'
}

# Each lookup fails LOUD (exit 2, no token) rather than defaulting to "no hold" —
# see the header's tooling-error rationale. Ordered PR-then-issue and placed
# ahead of every other probe so a hold applied while CI is still running lands
# before the loop can ever act on the green that follows.
pr_labels="$(labels_of pr "$pr")" ||
  die "could not read labels of PR #$pr; refusing to guess whether $OPTOUT_LABEL is set"
if has_optout_label "$pr_labels"; then
  echo "optout"; exit 0
fi

pr_body="$(gh pr view "${gh_args[@]}" --json body --jq '.body')" ||
  die "could not read the body of PR #$pr; refusing to guess whether a linked issue carries $OPTOUT_LABEL"
issue_n="$(linked_issue "$pr_body")"
if [[ -n "$issue_n" ]]; then
  issue_labels="$(labels_of issue "$issue_n")" ||
    die "could not read labels of issue #$issue_n (linked by PR #$pr); refusing to guess whether $OPTOUT_LABEL is set"
  if has_optout_label "$issue_labels"; then
    echo "optout"; exit 0
  fi
fi

# --- CI state from the exit code, not the text table -----------------------
ci_ec=0
# `2>&1 >/dev/null`: route stderr into the capture, then drop stdout — so ci_err
# holds gh's stderr (e.g. the no-checks-yet message) while the checks table is
# discarded. The `|| ci_ec=$?` guard absorbs gh's non-zero exit under `set -e`.
ci_err="$(gh pr checks "${gh_args[@]}" 2>&1 >/dev/null)" || ci_ec=$?
if [[ "$ci_ec" -eq "$CHECKS_PENDING_EC" ]]; then
  echo "pending"; exit 0
elif [[ "$ci_ec" -ne 0 ]]; then
  # No check runs registered yet (gh exits non-zero with a "no checks
  # reported" stderr) is a not-yet-started PR, not a failure → pending.
  # Herestring, not `printf … | grep -q`: under `pipefail` that pipeline can
  # report non-zero ON A MATCH (grep -q exits at the first hit, the writer dies
  # with SIGPIPE 141) — the same inversion the opt-out and rollup helpers below
  # avoid, and that .github/workflows/code-review.yml documents. Benign at this
  # input size today; the point is that the file has one rule, not two.
  if grep -qi "$NO_CHECKS_SIG" <<<"$ci_err"; then
    echo "pending"
  else
    echo "ci-failed"
  fi
  exit 0
fi

# --- CI is green: check mergeability + a FRESH LGTM verdict -----------------
# One call yields "<mergeStateStatus>|<HEAD committedDate>|<HEAD author login>",
# another the latest top-level verdict as "<createdAt>|<isLGTM>". gh applies --jq
# server-side. The HEAD author rides along here rather than in its own call: it is
# only needed by `review_gate_absent`, and `gh` already hands us the commit.
# (`gh` caps `commits` at 100. That is already how `head_date` is derived, and it
# fails CLOSED for the author too: on an adopted lane the bot's bump is commit 1
# and ours follow, so a truncated tail can only ever read as one of OURS.)
merge_line="$(gh pr view "${gh_args[@]}" \
  --json mergeStateStatus,commits \
  --jq '(.mergeStateStatus // "") + "|" + (.commits[-1].committedDate // "") + "|" + (.commits[-1].authors[0].login // "")')"
# Split by FIELD COUNT, not by seeking a separator: an enum, an RFC3339 stamp and
# a login can none of them contain `|`, so a surplus field means the answer is
# malformed — blanked here so every branch below fails closed on it. (Seeking one
# end instead is the `|`-injection class already proven exploitable once.)
IFS='|' read -r merge_state head_date head_author merge_rest <<<"$merge_line"
[[ -z "$merge_rest" ]] || { merge_state=""; head_date=""; head_author=""; }

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

# True when every comma-separated conclusion is SKIPPED. Walked by parameter
# expansion rather than a `printf | tr | grep -q` pipeline, because grep -q
# closing the pipe early is a SIGPIPE/pipefail inversion in the one test gating
# unreviewed merges. The appended comma makes a trailing empty field — a
# still-queued run — a value that must MATCH rather than one word splitting drops.
all_conclusions_skipped() {
  local remaining="$1," entry
  while [[ -n "$remaining" ]]; do
    entry="${remaining%%,*}"
    [[ "$entry" == "$SKIPPED_CONCLUSION" ]] || return 1
    remaining="${remaining#*,}"
  done
}

# True only when this PR has no review gate to wait for: Dependabot authored it,
# Dependabot also pushed its HEAD commit (so nothing of ours rides the branch —
# see the force-push note in the header), at least one non-review check actually
# SUCCEEDED (so "green" is not "nothing ran"), and every `claude-review` entry in
# its rollup reported SKIPPED (the rollup carries one entry per triggering event,
# so a single non-SKIPPED entry means the job did run and a verdict is genuinely
# owed). EVERY failure path fails CLOSED to `awaiting-review`: a failed call, an
# empty author, a malformed answer, or no `claude-review` entry at all all read
# as "the gate exists", so an unreadable answer can only ever hold the lane.
review_gate_absent() {
  local line author conclusions passes rest
  line="$(gh pr view "${gh_args[@]}" --json author,statusCheckRollup \
    --jq "(.author.login // \"\") + \"|\" + ([.statusCheckRollup[]? | select((.name // \"\") == \"$REVIEW_CHECK_NAME\") | (.conclusion // \"\")] | join(\",\")) + \"|\" + ([.statusCheckRollup[]? | select((.name // \"\") != \"$REVIEW_CHECK_NAME\" and (.conclusion // \"\") == \"$SUCCESS_CONCLUSION\")] | length | tostring)" \
    2>/dev/null)" || return 1
  # Field count again, for the same reason: a login, a list of enum conclusions
  # and a count can none of them contain `|`, so a surplus field is a malformed
  # answer — and no `|` in any value can shift the fields under us, which seeking
  # either end of the string would allow.
  IFS='|' read -r author conclusions passes rest <<<"$line"
  [[ -z "$rest" ]] || return 1
  [[ "$author" == "$DEPENDABOT_AUTHOR" ]] || return 1
  # $head_author came from the mergeStateStatus,commits call above — no extra API
  # round trip, and its empty default fails closed exactly like the rest.
  #
  # It is commit-AUTHOR metadata, not the push actor: `git commit --author` sets
  # it freely, and GitHub resolves a `…+dependabot[bot]@users.noreply.github.com`
  # address to that login without proving the pusher owns the account. So this
  # line is NOT the security boundary — do not later drop a sibling check
  # believing it is. Two things make it safe in composition: `.author.login`
  # above is the PR's creator, which only GitHub's own App attribution can set to
  # `app/dependabot`; and `.github/workflows/code-review.yml` keys its skip on
  # `github.actor`, the real authenticated pusher — so a human pushing a
  # spoofed-author commit onto a bot branch still triggers a real `claude-review`
  # run, whose non-SKIPPED rollup entry fails the check below. What this line
  # adds is the force-push case: it stops a `@dependabot recreate` from handing
  # back a fresh all-SKIPPED rollup that re-clears our adaptation commits.
  [[ "$head_author" == "$DEPENDABOT_COMMIT_AUTHOR" ]] || return 1
  [[ "$passes" =~ ^[0-9]+$ ]] || return 1
  [[ "$passes" -ge "$MIN_NON_REVIEW_SUCCESSES" ]] || return 1
  [[ -n "$conclusions" ]] || return 1
  all_conclusions_skipped "$conclusions"
}

# True only when the compare API proves the head is 0 commits behind its base.
# Fails CLOSED: an API error, an empty answer, or a non-integer all read as "not
# current", because `behind`'s remedy (fleet.sh sync) is always safe and a false
# `ready` is not.
branch_is_current() {
  local ref_line base head_oid slug behind
  ref_line="$(gh pr view "${gh_args[@]}" --json baseRefName,headRefOid \
    --jq '(.baseRefName // "") + "|" + (.headRefOid // "")' 2>/dev/null)" || return 1
  # Demand the separator before splitting on it. Without this, a separator-free
  # answer leaves BOTH `${ref_line%|*}` and `${ref_line##*|}` equal to the whole
  # string, so a base ref that happened to look like a short SHA would pass the
  # OID check below and be compared against ITSELF — `behind_by: 0`, a false
  # `ready`. jq always emits the `|`, so this is unreachable today; it is here
  # because "the answer is malformed" must never be able to mean "up to date".
  [[ "$ref_line" == *"|"* ]] || return 1
  # Split on the LAST separator: a SHA cannot contain `|` but a branch name can,
  # so seeking the first one would truncate a legitimately pipe-named base ref.
  base="${ref_line%|*}"
  head_oid="${ref_line##*|}"
  [[ -n "$base" ]] || return 1
  [[ "$head_oid" =~ ^[0-9a-f]{7,40}$ ]] || return 1
  slug="$CURRENT_REPO_SLUG"
  [[ -z "$repo_slug" ]] || slug="$repo_slug"
  behind="$(gh api "repos/$slug/compare/$base...$head_oid?per_page=1" \
    --jq '.behind_by' 2>/dev/null)" || return 1
  [[ "$behind" =~ ^[0-9]+$ ]] || return 1
  [[ "$behind" -eq 0 ]]
}

# Fresh LGTM ⇔ latest verdict is LGTM AND its createdAt is strictly newer than
# the HEAD commit. RFC3339 UTC timestamps are fixed-width, so a lexical string
# compare is a correct chronological compare (portable — no date arithmetic).
# Absent that the lane waits for review, unless there is provably no review to
# wait for — and the review-gate probe is LAZY for the same rate-limit reason as
# the compare probe: only a lane already lacking a fresh verdict ever pays for it.
ready_token="ready"
if [[ "$verdict_lgtm" != "true" || -z "$verdict_date" ]] || ! [[ "$verdict_date" > "$head_date" ]]; then
  review_gate_absent || { echo "awaiting-review"; exit 0; }
  ready_token="ready-unreviewed"
fi

# CLEAN is KEPT as well as the freshness probe: it is the only signal for
# DIRTY/CONFLICTING/BLOCKED/DRAFT/UNKNOWN, which `behind_by` cannot see. And the
# `&&` short-circuit is what keeps the compare call off every lane that is not
# already one step from merging — a non-CLEAN lane is going to `behind` either
# way, so paying an API request to learn how far behind it is, is pure waste.
if [[ "$merge_state" == "CLEAN" ]] && branch_is_current; then
  echo "$ready_token"
else
  echo "behind"
fi
