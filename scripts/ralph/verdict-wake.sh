#!/usr/bin/env bash
# scripts/ralph/verdict-wake.sh
#
# THE WAKE / MERGE-CLEARANCE SUMMARY. Composes and posts the four-line executive
# summary `.github/workflows/iteration-trigger.yml` fires on a PR once CI has
# completed and a Claude review has been posted. That comment is what wakes the
# Claude Code mobile app through its webhook, and its `**VERDICT**` line is a
# MERGE INSTRUCTION — `.claude/skills/await-claude-review/SKILL.md` Step 4a
# merges on `**VERDICT**:` plus `**CI**: x/y Green` and NOTHING else, and Step 3
# says that summary SHORT-CIRCUITS per-event classification, so when this path
# and `scripts/ralph/pr-ready.sh` disagree it is THIS one that wins.
#
# ---------------------------------------------------------------- WHY A SCRIPT
# This logic lived inside the workflow's `run:` block, and that is where its
# guards went to die. A `run:` body is never EXECUTED by any test: the only
# defence available against it was per-line `grep` over the YAML, and every
# static guard on a script nobody runs is evadable by keeping the guarded line
# alive and making it dead. MEASURED, against the fully green suite that
# preceded this file:
#
#   * keeping the real `-f scripts/ralph/lib/verdict-select.jq comments.json`
#     call on a live line but assigning it to an UNREAD variable, while setting
#     the real `ANSWER` from an inline selector that emits an unconditional
#     clearance, left every assertion in the repo passing while the wake step
#     cleared EVERY PR — no allowlist, no edit check, no null guard, no marker
#     check. #1266, #1199, #1263 and #1270 reopened at once.
#   * the reshape guard greps for a `{comments: …}` object-construction literal,
#     so writing the projection IN PLACE — a `map()` over the nodes path keeping
#     `body`, `createdAt`, `author` and `databaseId` — strips
#     `userContentEdits`, reopens #1263 and stays green.
#
# Both mutants change OBSERVABLE OUTPUT, and neither could be seen because there
# was no output to observe. As a script on disk it is executable, so
# `scripts/ralph/test_verdict_wake.sh` drives it end to end against fixtures
# with a stubbed `gh` — exactly as `scripts/ralph/test_pr_ready.sh` drives
# `pr-ready.sh`, where the same decoy mutant produced 60 failures. The static
# guards are KEPT as a cheap second line; they are no longer the only line.
#
# ------------------------------------------------------------------- CONTRACT
# ENV (all required; a missing one is a configuration error, not a wait):
#   GH_TOKEN  the PAT the summary is authored as. `iteration-trigger.yml` sets
#             it from `secrets.GEOFFE_GA_PAT` with NO `secrets.GITHUB_TOKEN`
#             fallback, because await-claude-review's recognition list names
#             exactly one author. Read by `gh`, never by this script.
#   REPO      `owner/name`.
#   PR        the pull request number.
#   SHA       the head SHA whose check-runs are tallied and whose commit date
#             the selected verdict must postdate.
#   MARKER    the summary's own first line, `<!-- iteration-trigger -->`. It is
#             both the self-post cap's needle and the bytes `pr-ready.sh`'s
#             ITER_SUMMARY_RE excludes from its verdict selector.
#
# OUTPUT — one of four terminal shapes, and only the last of them posts:
#   1. `Cap reached (N/10) - skipping`                         exit 0, no post
#   2. `No Claude review from an accepted author yet - skipping` exit 0, no post
#   3. `The selected verdict carried no usable comment id ('X') - skipping`
#                                                              exit 0, no post
#   4. a four-line comment posted with `gh pr comment`.
# Plus, on any refused verdict and BEFORE shapes 2-4, one `::warning::` line
# naming what was refused.
#
# THE FIVE INVARIANTS. This file emits a merge instruction, so it must enforce
# the same invariants as `scripts/ralph/pr-ready.sh` or it is a second code path
# that falsifies it. Three of them are no longer mirrored but SHARED (#1266,
# #1263) — the marker parse, the author allowlist and the edit check all live in
# `scripts/ralph/lib/verdict-select.jq`, which this script and `pr-ready.sh`
# both run over the payload `scripts/ralph/lib/pr-comments.graphql` both fetch.
# What this file still owns is the CLEARANCE CHAIN: the `do-not-auto-merge`
# hold, the marker-names-THIS-PR check, the base-currency check, the
# verdict-postdates-the-head check, and which blocker to name. All fail closed.
#
# AND EVERY BRANCH THAT REFUSES REWRITES `VERDICT`, NOT JUST `ACTION` (#1202).
# `VERDICT` is computed from the review comment ABOVE the chain, so a branch
# that refuses while leaving it alone still posts `**VERDICT**: LGTM` +
# `**CI**: N/N Green` — the only two fields the merge path reads. `ACTION` is
# diagnostic; `VERDICT` is the gate. The refusal values are chosen for their
# BYTES — `HELD`, `NOT ATTESTED`, `NOT CURRENT`, `STALE`, `DISPUTED` — none
# containing an `LGTM` substring and none being one of the three verdicts
# SKILL.md recognises, so Step 4a falls to its item 5 and surfaces to a human
# instead of merging. `scripts/ralph/test_pr_ready.sh` checks that property
# STRUCTURALLY, over every branch of the chain including ones added later, and
# separately that SKILL.md names each value this file can emit.
#
# Run:  REPO=owner/name PR=123 SHA=abc MARKER='<!-- iteration-trigger -->' \
#         bash scripts/ralph/verdict-wake.sh
set -euo pipefail

die() { printf 'verdict-wake: %s\n' "$1" >&2; exit 2; }

# A missing variable is a BROKEN CALLER, not a wait: `set -u` would abort with a
# bash-shaped message naming a variable and nothing about why it mattered, on a
# path whose silence is exactly what #1270 exists to close.
for _required in REPO PR SHA MARKER; do
  [[ -n "${!_required:-}" ]] ||
    die "\$$_required is empty or unset; refusing to compose a merge instruction without it"
done
case "$REPO" in
  */*) : ;;
  *)   die "\$REPO ('$REPO') is not owner/name; \`gh api graphql\` substitutes no placeholders and would ask the API for that literal" ;;
esac

# The shared verdict machinery, SPELLED REPO-RELATIVE AND RESOLVED
# SCRIPT-RELATIVE, exactly as `pr-ready.sh` spells and resolves it and for the
# same two reasons: the repo-relative form is the one the coupling tests grep
# out of both consumers, and the script-relative resolution is what lets a copy
# of this script find the lib that ships beside it.
readonly VERDICT_FILTER_REL='scripts/ralph/lib/verdict-select.jq'
readonly VERDICT_QUERY_REL='scripts/ralph/lib/pr-comments.graphql'
# Declared and assigned separately: `readonly X="$(cmd)"` makes the assignment
# the `readonly` builtin's status, which masks a failing `cd` (SC2155).
RALPH_LIB_DIR="$(cd "$(dirname "$0")" && pwd)/lib" ||
  die "could not resolve this script's own directory to find its lib/"
readonly RALPH_LIB_DIR
readonly VERDICT_FILTER="$RALPH_LIB_DIR/${VERDICT_FILTER_REL##*/}"
readonly VERDICT_QUERY="$RALPH_LIB_DIR/${VERDICT_QUERY_REL##*/}"
[[ -s "$VERDICT_FILTER" ]] ||
  die "the shared verdict filter is missing or empty ($VERDICT_FILTER); refusing to clear a merge with no selector"
[[ -s "$VERDICT_QUERY" ]] ||
  die "the shared comment query is missing or empty ($VERDICT_QUERY); refusing to clear a merge with no payload"

# ONE PAYLOAD, SHARED WITH pr-ready.sh (#1263). This step used to read
# `repos/$REPO/issues/$PR/comments`, which carries no edit history at all — so
# the question "who actually wrote this body" was unanswerable here by any
# tightening of the jq, and the two clearance paths also disagreed about the
# field name (`.user.login`) and the bot's login spelling
# (`github-actions[bot]`), a divergence trap this file used to spend forty lines
# describing. Reading the same GraphQL payload as pr-ready.sh deletes the trap
# instead of documenting it.
#
# THE ANSWER IS STORED RAW — NO `--jq` RESHAPE, AND NO `map()` OVER THE NODES
# EITHER. Both consumers used to project it to `{comments: …}` first: two
# hand-mirrored reshapes feeding one shared selector, which is the exact shape
# #1266 came out of. Neither was reachable from the selector's own tests (they
# all feed a hand-built envelope), so narrowing either projection — to
# `{body, createdAt, author}`, or by rewriting the nodes IN PLACE with a `map()`
# that keeps every field a grep knows to look for — strips `userContentEdits`,
# reopens #1263 in full, and leaves both suites green.
# `scripts/ralph/lib/verdict-select.jq` accepts the raw answer itself, so there
# is ONE intake and it lives in the program that reads the fields.
# `scripts/ralph/test_verdict_wake.sh`'s foreign-editor case is the behavioural
# pin: any reshape that loses the edit history flips it from a refusal to a
# clearance.
gh api graphql \
  -f query="$(cat "$VERDICT_QUERY")" \
  -f owner="${REPO%%/*}" \
  -f name="${REPO##*/}" \
  -F number="$PR" \
  > comments.json

# Cap: stop after 10 prior self-posts on this PR. Counted over the PARSED nodes,
# not with `grep -c` over the raw response: `gh api` emits its JSON on one line,
# so a line count of a marker that appears ten times answers 1 and the cap could
# never trip. `[]?` so a payload whose shape we cannot walk yields 0 rather than
# aborting under `set -euo pipefail` — the same payload also selects no verdict,
# so the early exit below is where such a run ends.
PRIOR=$(jq --arg m "$MARKER" '[.data.repository.pullRequest.comments.nodes[]? | select((.body // "") | contains($m))] | length' comments.json)
if [ "$PRIOR" -ge 10 ]; then
  echo "Cap reached ($PRIOR/10) - skipping"
  exit 0
fi

# THE VERDICT SELECTOR IS scripts/ralph/lib/verdict-select.jq, THE SAME FILE
# pr-ready.sh READS. This step used to hold its own copy, and that duplication
# is the defect rather than a side effect of it: #1266 is a `.body != null`
# guard that was written into pr-ready.sh's copy and not this one, so a single
# null-bodied comment aborted this step under `set -euo pipefail` and the lane
# silently lost its wake on every subsequent CI completion. #1199's author
# allowlist had to be added to each copy separately for exactly the same reason.
#
# THE ANSWER IS CONSUMED, NOT MERELY PRODUCED. `ANSWER` is read by the split
# immediately below and by nothing else, and every field of every branch after
# it descends from that split — which is what makes the decoy mutant (a live
# invocation whose result is thrown away beside an inline selector that clears)
# a change in observable output rather than an invisible one.
#
# The parameters are passed with `--arg`, which hands each value over byte for
# byte. They are the constants `scripts/ralph/pr-ready.sh` declares — VERDICT_RE,
# VERDICT_LGTM_RE, ITER_SUMMARY_RE, MARKER_RE, MARKER_ANY_RE, MARKER_MALFORMED
# and VERDICT_AUTHORS_JQ — and `scripts/ralph/test_pr_ready.sh` compares them
# across the two files, because a regex that drifts here matches nothing and
# unmarks every lane at once, silently.
#
# ONE SPELLING OF THE ALLOWLIST NOW, not two. The GraphQL payload renders the
# Actions bot as the bare slug `github-actions`; the REST payload this script no
# longer reads spelled it `github-actions[bot]`. Both files read the same
# payload, so both carry the same literal.
ANSWER=$(jq -r \
  --argjson authors '["Geoffe-Ga","github-actions"]' \
  --arg verdict_re '(?im)^\s*(?:#{1,6}\s+|\*\*)?verdict[:*\s]' \
  --arg verdict_lgtm_re '(?im)^(?:#{1,6}[ \t]+|\*\*)?verdict[:*\s]+lgtm[*\s]*$' \
  --arg iter_summary_re '(?m)^<!-- iteration-trigger -->[[:space:]]*$' \
  --arg marker_re '(?m)^<!-- creek-review pr=([0-9]+) -->[[:space:]]*$' \
  --arg marker_any_re 'creek-review' \
  --arg marker_malformed 'malformed' \
  -f "$VERDICT_FILTER" comments.json)

# Split by FIELD COUNT, exactly as pr-ready.sh does: a stamp, a jq boolean, a PR
# number, a login and a decimal comment id can none of them contain `|`, so a
# SIXTH field means the answer is not the shape we asked for and every branch
# below must fail closed on it rather than reading a shifted value.
IFS='|' read -r VDATE VLGTM MARKER_PR VREFUSED VID VREST <<<"$ANSWER"
if [ -n "$VREST" ]; then
  VDATE=""; VLGTM=""; MARKER_PR=""; VREFUSED=""; VID=""
fi

# The refusal diagnostic is printed BEFORE the early exit, because filtering at
# selection makes a skipped verdict invisible: without this the lane behaves
# exactly as if nothing had been posted, which is the wrong report when what
# happened is that the PAT was rotated (#1199) or a body was rewritten by
# somebody other than its author (#1263).
if [ -n "$VREFUSED" ]; then
  echo "::warning::The latest verdict-bearing comment on PR #${PR} was not admitted: ${VREFUSED}. See scripts/ralph/lib/verdict-select.jq for the two refusal shapes."
fi

if [ -z "$VDATE" ]; then
  # No verdict from an accepted author. Either none has been posted yet (the
  # ordinary case) or the only verdict-shaped comments came from accounts this
  # pipeline cannot post as, or one was tampered with — #1199 and #1263 reaching
  # their dead end: no summary is composed, so nothing ever says "cleared to
  # squash merge" on the strength of it.
  echo "No Claude review from an accepted author yet - skipping"
  exit 0
fi

# The selected comment itself, addressed by the `databaseId` THE SHARED FILTER
# RETURNED — never re-found by `createdAt`, which is what this did and which was
# wrong. GitHub's stamps are second-granular and two comments landing in the
# same second are ordinary on an active PR, so `[… | select(.createdAt == $ts)]
# | last` over the UNFILTERED list can answer with a comment the filter REFUSED:
# the wake message would name the wrong comment and the DISPLAYED verdict would
# be read off a body the gate rejected. That contradicts the filter's own
# doctrine — "every field is read off the ONE selected comment, so no field can
# be supplied by a comment the gate refused" — and the fix is to stop looking
# the comment up twice. A `databaseId` is unique.
#
# ID MUST STAY THE NUMERIC databaseId, not the opaque GraphQL node id: it is
# printed below as "pull comment ${ID} to see in-depth feedback", which a human
# follows into GitHub's own comment URLs.
#
# FAILS CLOSED ON AN UNUSABLE ID: anything that is not a bare decimal means we
# cannot name the comment the gate admitted, and composing a summary around a
# comment we cannot identify is precisely what this removes. That is the same
# dead end as "no verdict found" — no summary, so nothing ever says "cleared to
# squash merge".
case "$VID" in
  ''|*[!0-9]*)
    echo "The selected verdict carried no usable comment id ('${VID}') - skipping"
    exit 0 ;;
esac
ID="$VID"
SELECTED=$(jq -c --argjson id "$VID" '[.data.repository.pullRequest.comments.nodes[]? | select(.databaseId == $id)] | last // {}' comments.json)
BODY=$(jq -r '.body // ""' <<<"$SELECTED")

# PROVENANCE (#1181) COMES FROM THE SHARED FILTER'S THIRD FIELD, read off the
# SAME comment the verdict came from — the parity requirement in this file's
# header, enforced by construction rather than by two files agreeing about a
# regex. code-review.yml prepends `<!-- creek-review pr=N -->` with N taken from
# the workflow's own event payload, so a verdict without it came from the
# pipeline that reviewed PR #1179 and posted the LGTM onto #1117.
#
# The whole-LINE anchoring, the `[[:space:]]*` carriage-return tolerance and the
# take-the-FIRST-marker rule all live in scripts/ralph/lib/verdict-select.jq, so
# this path cannot drift from pr-ready.sh's about which bytes attest. Two values
# that are not a bare PR number can arrive here — the empty string (no marker)
# and `malformed` (the body mentions the marker but carries no parseable one) —
# and NEITHER can equal "$PR", so the clearance guard below fails closed on both.

# The DISPLAYED verdict. `LGTM` is decided by the filter's own flag, which is the
# field the clearance chain gates on; the other two are a presentation detail, so
# they are read off the selected body. Grepping the verdict LINE and not the
# whole body, because a COMMENTS rationale may itself mention "LGTM"; the LAST
# such line is the final word, the same contract as stats.py's normalize_verdict.
#
# THAT LAST-LINE CONTRACT HOLDS ON THE `LGTM` ARM TOO, and it did not when this
# branch was first written. `$VLGTM` used to be a WHOLE-BODY
# `test($verdict_lgtm_re)` — "does ANY line say LGTM" — and `VERDICT_RE` admits
# leading whitespace, so a review that QUOTES `    ## Verdict: LGTM` while itself
# concluding `## Verdict: CHANGES_REQUESTED` came out of the filter as `true` and
# this step posted "You are cleared to squash merge" on it. On a PR discussing
# these very files that is not a hypothetical. The flag is computed off the LAST
# verdict-shaped line in scripts/ralph/lib/verdict-select.jq now, so both
# clearance paths and stats.py's normalize_verdict agree by construction.
#
# AND THE STRICT GREP IS NOW COMPUTED FOR BOTH ARMS SO IT CAN DISSENT. It used to
# run only inside the `else`, which meant the one line in this file spelled the
# way `.github/workflows/code-review.yml` ACTUALLY WRITES a verdict
# (`printf '\n\n## Verdict: %s\n'`, column 0, enum-constrained token) could never
# contradict the flag — it was consulted only once the flag had already conceded.
# It is a cross-check, not a formatter, so it is read first and the two must
# agree before anything says LGTM. An EMPTY `$VLINE` is not a disagreement: the
# legacy `## Verdict\nLGTM` shape carries no `^## Verdict:` line at all, and
# treating "this file could not see one" as a veto would unmark those lanes.
VLINE=$(grep -E '^## Verdict:' <<<"$BODY" | tail -n 1 || true)
if [ "$VLGTM" = "true" ]; then
  case "$VLINE" in
    *CHANGES_REQUESTED*|*COMMENTS*)
      # The filter cleared a body whose own last `## Verdict:` line refuses.
      # `DISPUTED` carries no `LGTM` substring and is none of the three verdicts
      # `.claude/skills/await-claude-review/SKILL.md` recognises, so Step 4a
      # falls to its item 5 and surfaces to a human — the same fail-closed
      # destination as `HELD`, `NOT ATTESTED` and `NOT CURRENT`, and the
      # enclosing `[ "$VERDICT" = "LGTM" ]` below therefore never fires.
      VERDICT='DISPUTED' ;;
    ''|*LGTM*)
      VERDICT='LGTM' ;;
    *)
      VERDICT='DISPUTED' ;;
  esac
else
  case "$VLINE" in
    *CHANGES_REQUESTED*) VERDICT='CHANGES REQUESTED' ;;
    *)                   VERDICT='COMMENTS' ;;
  esac
fi

# CI status from check-runs on the head SHA. Skipped/neutral runs are
# non-blocking (e.g. a job that's conditionally skipped on this PR) and would
# otherwise keep GREEN < TOTAL forever, so the merge clearance below could never
# fire. In-progress runs (conclusion null) still count in TOTAL, so nothing is
# cleared while a check is running.
gh api "repos/$REPO/commits/$SHA/check-runs?per_page=100" > runs.json
TOTAL=$(jq '[.check_runs[] | select(.conclusion != "skipped" and .conclusion != "neutral")] | length' runs.json)
GREEN=$(jq '[.check_runs[] | select(.conclusion == "success")] | length'                             runs.json)

# Green + LGTM is NOT enough to clear a merge, and this job used to clear on
# those two alone. That made it a second code path able to falsify the invariant
# scripts/ralph/pr-ready.sh enforces: it would tell the loop to merge the very
# lanes that helper refuses — a branch behind its base (whose own green proves
# nothing about today's main) and one a human put on hold with
# `do-not-auto-merge`. Both checks below fail CLOSED: a missing/unreadable PR
# object yields no labels (HELD="unknown") and an empty BEHIND, neither of which
# clears.
PR_JSON=$(gh api "repos/$REPO/pulls/$PR" 2>/dev/null || echo '{}')
BASE=$(jq -r '.base.ref // empty' <<<"$PR_JSON")
HELD=$(jq -r 'if .labels == null then "unknown"
              elif ([.labels[].name] | index("do-not-auto-merge")) then "yes"
              else "no" end' <<<"$PR_JSON")
# behind_by counts base commits the head lacks. GitHub only reports
# mergeStateStatus BEHIND when the base enforces strict status checks, which
# this repo does not — so a branch many commits behind main reads CLEAN and only
# this compare call can see it. Anything but a literal "0", including the empty
# string a failed or malformed compare leaves, is "not current".
BEHIND=$(gh api "repos/$REPO/compare/$BASE...$SHA?per_page=1" --jq '.behind_by' 2>/dev/null || true)

# THE VERDICT MUST POSTDATE THE HEAD IT IS CLEARING, and nothing here compared
# them. `pr-ready.sh` has enforced this since #1181 (`[[ "$verdict_date" >
# "$head_date" ]]`, its stale-verdict guard); this path — the one
# await-claude-review Step 3 says SHORT-CIRCUITS per-event classification, so it
# is the one that wins when the two disagree — cleared on `$VLGTM` and CI colour
# alone. `code-review.yml` normally hides that: every push cancels the in-flight
# review and starts a new one, so a verdict is almost always newer than HEAD by
# construction. `workflow_dispatch` breaks the "almost": an operator dispatches a
# review onto a Dependabot PR, it posts an approving verdict, Dependabot then
# rebases, and the summary cleared the NEW head on the OLD head's review — while
# CI, re-run on the new head, is green and says nothing about it.
#
# The comparison is the same one pr-ready.sh makes and for the same reason it
# makes it that way: both stamps are RFC3339 in UTC with a literal `Z`, a
# fixed-width format whose lexical order IS its chronological order, so a string
# compare needs no date parsing (and `date -d` is not portable to the BSD `date`
# on a developer's macOS anyway). `committer.date` is the field that moves on a
# rebase or an amend; `author.date` can be carried over from the original commit
# and would read as unchanged across exactly the force-push this guard exists for.
#
# FAILS CLOSED: an unreadable commit leaves HEAD_DATE empty, and the guard below
# refuses on an empty HEAD_DATE rather than comparing against it — `>` against
# the empty string is true for every non-empty stamp, which would have made an
# API hiccup clear the merge.
HEAD_DATE=$(gh api "repos/$REPO/commits/$SHA" --jq '.commit.committer.date' 2>/dev/null || true)

if [ "$GREEN" = "$TOTAL" ] && [ "$VERDICT" = "LGTM" ]; then
  if [ "$HELD" != "no" ]; then
    # THE ONE CONTROL A HUMAN RETAINS over an autonomous merge loop, so this
    # branch is the most important place in the file to get right — and until
    # #1202 it was the one that failed. Rewriting ACTION alone left the summary
    # asserting `**VERDICT**: LGTM`, which is half of what Step 4a merges on, so
    # a webhook-woken session merged the very PR a human had parked. `HELD`
    # carries no `LGTM` substring and is not a verdict SKILL.md recognises; see
    # the header for the full argument about why the VERDICT field, not ACTION,
    # is the gate.
    VERDICT='HELD'
    ACTION="NOT cleared to merge: the do-not-auto-merge hold is set on this PR (or its labels could not be read). A human owns this one - leave it alone."
  elif [ "$MARKER_PR" != "$PR" ]; then
    # Fails CLOSED: an absent or unparseable marker leaves MARKER_PR empty,
    # which never equals a PR number, so it does not clear.
    #
    # REWRITING `VERDICT` IS WHAT ACTUALLY STOPS THE MERGE; `ACTION` IS NOT
    # LOAD-BEARING — THE RULE FOR EVERY BRANCH OF THIS CHAIN, argued here once
    # and referenced from the other two.
    # `.claude/skills/await-claude-review/SKILL.md` Step 4a parses
    # `**VERDICT**:` and `**CI**: x/y Green` and nothing else — its item 3
    # returns LGTM to the caller on those two fields alone, and its item 5 says
    # in as many words "Do not infer a verdict from the `Action:` prose".
    # VERDICT is computed from the review comment ABOVE this clearance chain,
    # and the chain only ever rewrote ACTION, so a marker-mismatched PR still
    # posted `**VERDICT**: LGTM` + `**CI**: N/N Green` and a woken session merged
    # on it — while Step 3 makes this summary SHORT-CIRCUIT per-event
    # classification, so it is exactly the half that wins over pr-ready.sh's
    # `awaiting-review`. A comment must not assert a merge-clearing verdict that
    # the same comment then refuses.
    #
    # `NOT ATTESTED` is chosen for its BYTES: it contains no `LGTM` substring, so
    # neither a `*LGTM*` glob nor a downstream `test("LGTM")` can match it, and
    # it is none of the three verdicts SKILL.md's recognition rule accepts — so
    # Step 4a falls to item 5 (marker present, verdict unparseable → surface to
    # the user), which is the destination we want: a human looks, nobody merges.
    # Assigning it here is safe because the enclosing
    # `[ "$GREEN" = "$TOTAL" ] && [ "$VERDICT" = "LGTM" ]` has already been
    # evaluated, and nothing reads VERDICT again before the BODY printf below.
    #
    # THE `HELD` AND `BEHIND` BRANCHES DO THE SAME THING (#1202). They carried
    # the identical hole for as long as this branch did not: rewriting ACTION
    # alone, so `**VERDICT**: LGTM` stood and a `do-not-auto-merge` hold was
    # defeated exactly the way an unattested verdict was. The asymmetry was
    # scoped to #1181 on purpose and is now closed; the chain has ONE rule, and
    # test_pr_ready.sh enforces it structurally rather than by naming these three
    # branches, so a fourth refusal inherits it.
    VERDICT='NOT ATTESTED'
    ACTION="NOT cleared to merge: the latest verdict does not attest that the reviewer read THIS PR - its provenance marker names '${MARKER_PR}', not ${PR}. Run scripts/ralph/fleet.sh sync (or push any commit) so the marker-emitting review workflow runs on this branch and the re-review is attested; re-running the old review run will not help, it replays the workflow file from the commit that run was launched from. See #1181."
  elif [ "$BEHIND" != "0" ]; then
    # Same rule, same reason (#1202): this branch's own green proves nothing
    # about today's base, and pr-ready.sh refuses the lane for it — so the
    # summary must not hand the merge path an `LGTM` the sibling clearance path
    # would reject. `NOT CURRENT` carries no `LGTM` substring and is not a
    # verdict SKILL.md recognises.
    VERDICT='NOT CURRENT'
    ACTION="NOT cleared to merge: this head is not current with ${BASE:-its base} (behind_by='${BEHIND}'). Run scripts/ralph/fleet.sh sync and let CI re-run first."
  elif [ -z "$HEAD_DATE" ] || ! [[ "$VDATE" > "$HEAD_DATE" ]]; then
    # Same rule, same reason (#1202). `[[ ]]`, not `[ ]`: inside `[ ]` a bare `>`
    # is a REDIRECTION, so `[ "$VDATE" > "$HEAD_DATE" ]` would test one argument
    # for non-emptiness and silently create a file named after the head stamp —
    # true on every run, which is a guard that reads as present and is not.
    # `! [[ … ]]` rather than `<=`, so an EQUAL pair (a verdict posted in the same
    # second as the commit it is clearing) refuses: at second granularity that
    # ordering is unknowable, and unknowable must not clear.
    VERDICT='STALE'
    ACTION="NOT cleared to merge: the verdict (${VDATE}) does not postdate this head ${SHA} (${HEAD_DATE:-unreadable}) - it reviewed code that has since been replaced. Push or re-run the review workflow on the current head and wait for the fresh verdict."
  else
    ACTION="You are cleared to squash merge, delete the branch, clean any worktrees, and unsubscribe from webhooks. Please proceed."
  fi
else
  ACTION="pull comment ${ID} to see in-depth feedback and continue iterating"
fi

SUMMARY=$(printf '%s\n%s\n%s\n%s\n' \
  "$MARKER" \
  "**CI**: ${GREEN}/${TOTAL} Green" \
  "**VERDICT**: ${VERDICT}" \
  "**Action**: ${ACTION}")
# `gh pr comment`, not `gh api -X POST repos/.../issues/N/comments`. Same
# endpoint underneath and the same PAT author, but this file no longer names the
# REST issue-comments path anywhere — which is what
# `creek-tools/tests/test_verdict_clearance_parity.py` asserts, so that a
# re-introduced read of the payload with no edit provenance is caught by the
# same check that caught the write.
gh pr comment "$PR" -R "$REPO" --body "$SUMMARY"
