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
# Beyond that baseline, these tests pin seven more dimensions:
#
#   verdict split  the four verdict states are distinct outcomes (issue #1097):
#                  missing → awaiting-review, stale (LGTM or not) →
#                  awaiting-review, fresh LGTM → ready, fresh non-LGTM →
#                  changes-requested — with a malformed verdict answer failing
#                  closed to awaiting-review, never to the dispatch token.
#   opt-out        a `do-not-auto-merge` label on the PR — or on the LAST issue
#                  its body links — parks the lane (`optout`), checked BEFORE CI
#                  so a human hold is honoured even mid-run. An UNDETERMINABLE
#                  hold (any lookup failing) exits 2 and prints nothing, so the
#                  caller can never read silence as consent.
#   freshness      `ready` additionally requires the compare API's
#                  `behind_by == 0`. `mergeStateStatus == CLEAN` is NOT a
#                  freshness signal — GitHub happily reports UNSTABLE/MERGEABLE
#                  for a branch 22 commits behind main. Fails CLOSED (`behind`)
#                  on an API error, an empty answer, or a non-integer.
#   ready-unreviewed  a PR with PROVABLY no review gate (dependabot-authored PR
#                  AND a dependabot-authored HEAD commit AND a real non-review
#                  SUCCESS AND every `claude-review` entry exactly SKIPPED) may
#                  merge without a verdict. Anything less ⇒ `awaiting-review`.
#   laziness       both new probes cost an extra API call per wake per lane, so
#                  each must run ONLY on the path that would otherwise print
#                  `ready`/`ready-unreviewed`. Sentinel files prove it.
#   field counts   the multi-field `gh` answers are split with
#                  `IFS='|' read -r a b c rest`; a non-empty `rest` (a `|` in a
#                  branch name, a login, a date) must fail CLOSED, not silently
#                  shift a field and merge on garbage.
#   main health    the #1157 relaxation (a behind-but-inert lane merges anyway)
#                  is justified by ONE backstop: the full CI run on `push: main`
#                  re-proving every squash-merge. So a lane that is about to USE
#                  the relaxation must first prove the backstop is alive —
#                  `main-not-green` (issue #1159). A lane with `behind_by == 0`
#                  never uses the relaxation and therefore never asks.
#
# Plus one cross-file coupling check: pr-ready.sh matches the review check by the
# literal name `claude-review`, so `.github/workflows/code-review.yml` must keep
# that job key and must NOT add a `name:` override.
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
probed() { # probed <desc> <yes|no> <sentinel path> — did the probe run?
  if [[ -e "$3" ]]; then check "$1" "$2" "yes"; else check "$1" "$2" "no"; fi
}
no_merge_token() { # no_merge_token <desc> <token> — any token the loop won't merge on
  if [[ "$2" != "ready" && "$2" != "ready-unreviewed" ]]; then ok "$1"; else bad "$1 (got '$2')"; fi
}

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
BIN="$WORK/bin"
mkdir -p "$BIN"

# Arg-aware fake gh. Behaviour is driven by env vars the test sets per case:
#   CHECKS_EC        — exit code `gh pr checks` should return (0 green / 8 pending / other failed)
#   CHECKS_NO_CHECKS — when "1", `gh pr checks` fails with the exact stderr gh
#                      emits when NO check runs are registered yet on the
#                      branch ("no checks reported on the '<branch>' branch"),
#                      exit 1. This must classify as `pending`, not `ci-failed`
#                      — CI just hasn't started, not failed.
#   MERGE_STATE   — mergeStateStatus (CLEAN / BEHIND / ...)
#   HEAD_DATE     — RFC3339 committedDate of the PR HEAD commit
#   HEAD_AUTHOR   — login of the HEAD commit's author, the 3rd field of the
#                   mergeStateStatus answer. Defaulted with `-` (not `:-`) so a
#                   test can pin an EMPTY author too. Set it to a human login to
#                   prove a human commit pushed on top of a bot PR revokes the
#                   no-review-needed shortcut.
#   BEHIND_BY     — what the compare API reports for `.behind_by`. Defaulted with
#                   `-` (not `:-`) so `BEHIND_BY=''` reproduces an EMPTY answer,
#                   which must fail closed rather than compare equal to 0.
#   COMPARE_EC    — exit code of the compare call; a test sets 1 to prove a
#                   failed freshness probe yields `behind`, never `ready`.
#   COMPARE_SENTINEL — file the compare arm touches when it is called. The
#                   laziness tests assert the probe did NOT run on lanes that
#                   already decided (one wasted API call per lane per wake is
#                   how a loop burns its rate limit).
#   PR_LABELS     — comma-separated labels on the PR itself, emitted one per line
#                   the way `--jq '.labels[].name'` does.
#   PR_LABELS_EC  — exit code of that lookup; 1 proves an UNDETERMINABLE hold
#                   exits 2 and prints nothing (silence must not read as consent).
#   ISSUE_LABELS  — same, for the issue the PR body links.
#   ISSUE_LABELS_EC — exit code of the linked-issue label lookup.
#   ISSUE_LABELS_FOR — when set, ISSUE_LABELS is served ONLY for that issue
#                   number; every other number gets an empty answer. This is what
#                   pins "the LAST closes-link wins" against a Dependabot body
#                   whose upstream changelog is full of `Fixes #456` noise.
#   PR_BODY       — the PR body the `(closes|fixes|resolves) #N` regex reads.
#   PR_BODY_EC    — exit code of the body lookup.
#   BASE_REF / HEAD_OID — the `<base>|<headOid>` pair the compare URL is built from.
#   BASE_LINE_RAW — emitted verbatim in place of that pair (honoured even when
#                   empty), so a case can feed a malformed answer the well-formed
#                   template cannot express — e.g. one with no separator at all.
#   PR_AUTHOR     — `.author.login` for the review-gate probe; only the app slug
#                   `app/dependabot` can clear the gate.
#   REVIEW_CONCLUSIONS — comma-joined conclusions of the `claude-review` rollup
#                   entries. Every one must be exactly SKIPPED to prove no review
#                   ever gates this PR; `SKIPPED,SUCCESS` and a trailing-comma
#                   `SKIPPED,` (an entry still queued, conclusion null) must not.
#   NON_REVIEW_SUCCESSES — how many NON-review checks concluded SUCCESS; 0 means
#                   nothing actually passed, so there is nothing to merge on.
#                   Defaulted with `-` so a test can inject a surplus `|` field.
#   REVIEW_EC     — exit code of the review-gate lookup; 1 must fail closed.
#   REVIEW_SENTINEL — file the review-gate arm touches; laziness assertions.
#   ROLLUP_JSON   — raw `--json author,statusCheckRollup` payload; when set, the
#                   stub runs the REAL jq with pr-ready.sh's own `--jq`, so a
#                   rollup expression that miscounts SKIPPED/null conclusions is
#                   caught here (a scalar stub would mask it).
#   VERDICT       — the "<createdAt>|<isLGTM>" scalar the verdict jq resolves to
#   COMMENTS_JSON — raw `--json comments` payload; when set, the stub runs the
#                   REAL jq with pr-ready.sh's own `--jq` expression against it,
#                   so the production verdict regex is genuinely exercised
#                   (otherwise a scalar stub would mask a broken regex).
#   MAIN_HEALTH   — scalar shortcut for the `gh run list` arm that main-health.sh
#                   (pr-ready.sh's sibling, issue #1159) calls: `green` / `red` /
#                   `pending` emit one synthetic run line, `unknown` emits
#                   nothing at all. It DEFAULTS TO `green` so the pre-existing
#                   behind-but-inert cases keep testing #1157's disjointness
#                   rather than accidentally testing this gate.
#   MAIN_RUNS_JSON — raw `--json status,conclusion,headSha,databaseId,url`
#                   payload; when set, the stub runs the REAL jq with
#                   main-health.sh's own `--jq` expression against it (the
#                   COMMENTS_JSON / ROLLUP_JSON pattern), so the production
#                   run-list expression is genuinely exercised through
#                   pr-ready.sh rather than mocked at the token.
#   MAIN_HEALTH_EC — exit code of the run-list call; 1 proves a failed lookup
#                   holds the lane (`main-not-green`) and still exits 0.
#   MAIN_HEALTH_SENTINEL — file the run-list arm touches when it is called. The
#                   laziness assertions use it: this gate costs an extra API
#                   call, so only a lane about to USE the #1157 relaxation may
#                   pay for it.
# Real gh applies --jq, so — like test_fleet.sh — the stub emits the already
# extracted scalar and branches on which --json field the caller asked for.
cat > "$BIN/gh" <<'STUB'
#!/usr/bin/env bash
args="$*"
case "$args" in
  "run list"*)
    # main-health.sh's single call. One line per run, NEWEST FIRST, five
    # `|`-separated fields: status|conclusion|headSha|databaseId|url.
    [[ -n "${MAIN_HEALTH_SENTINEL:-}" ]] && : > "$MAIN_HEALTH_SENTINEL"
    if [[ -n "${MAIN_RUNS_JSON:-}" ]]; then
      expr="" prev=""
      for a in "$@"; do [[ "$prev" == "--jq" ]] && expr="$a"; prev="$a"; done
      printf '%s' "$MAIN_RUNS_JSON" | jq -r "$expr"
      exit "${MAIN_HEALTH_EC:-0}"
    fi
    case "${MAIN_HEALTH:-green}" in
      green)   printf 'completed|success|abc1234|11|https://x/11\n' ;;
      red)     printf 'completed|failure|dead1234|12|https://x/12\n' ;;
      pending) printf 'in_progress||feed1234|13|https://x/13\n' ;;
      *)       : ;;   # `unknown`: gh answered nothing at all
    esac
    exit "${MAIN_HEALTH_EC:-0}" ;;
  "api "*"compare"*)
    # Three compare calls now, told apart by the --jq the caller passes and by
    # the range. The behind_by/merge-base probe is the only one a lane that is
    # already current ever makes; the two file listings are paid only by a lane
    # that is genuinely behind.
    if [[ "$args" == *"behind_by"* ]]; then
      [[ -n "${COMPARE_SENTINEL:-}" ]] && : > "$COMPARE_SENTINEL"
      printf '%s|%s\n' "${BEHIND_BY-0}" "${MERGE_BASE-abc1234}"
      exit "${COMPARE_EC:-0}"
    fi
    [[ -n "${FILES_SENTINEL:-}" ]] && : > "$FILES_SENTINEL"
    # `<merge base>...<base>` is what MAIN landed; anything else is `<base>...<head>`,
    # what THIS BRANCH changed. Both are comma-separated in the fixture and split
    # to lines here, because a filename cannot contain a comma in these tests.
    if [[ "$args" == *"compare/${MERGE_BASE-abc1234}..."* ]]; then
      [[ -n "${THEIR_FILES-}" ]] && printf '%s' "${THEIR_FILES-}" | tr ',' '\n'
      exit "${THEIR_FILES_EC:-0}"
    fi
    [[ -n "${OUR_FILES-}" ]] && printf '%s' "${OUR_FILES-}" | tr ',' '\n'
    exit "${OUR_FILES_EC:-0}" ;;
  *"pr checks"*)
    if [[ "${CHECKS_NO_CHECKS:-0}" == "1" ]]; then
      echo "no checks reported on the 'feature-x' branch" >&2
      exit 1
    fi
    exit "${CHECKS_EC:-0}" ;;
  *"pr view"*"--json labels"*)
    printf '%s\n' "${PR_LABELS:-}" | tr ',' '\n'
    exit "${PR_LABELS_EC:-0}" ;;
  *"issue view"*"--json labels"*)
    n=""
    for a in "$@"; do
      if [[ -z "$n" && "$a" =~ ^[0-9]+$ ]]; then n="$a"; fi
    done
    if [[ -z "${ISSUE_LABELS_FOR:-}" || "${ISSUE_LABELS_FOR}" == "$n" ]]; then
      printf '%s\n' "${ISSUE_LABELS:-}" | tr ',' '\n'
    fi
    exit "${ISSUE_LABELS_EC:-0}" ;;
  *"pr view"*"--json body"*)
    printf '%s\n' "${PR_BODY:-}"
    exit "${PR_BODY_EC:-0}" ;;
  *"pr view"*"--json baseRefName"*)
    if [[ -n "${BASE_LINE_RAW+set}" ]]; then printf '%s\n' "$BASE_LINE_RAW"; exit 0; fi
    printf '%s|%s\n' "${BASE_REF:-main}" "${HEAD_OID:-c0ffee1}" ;;
  *"pr view"*"--json author"*)
    [[ -n "${REVIEW_SENTINEL:-}" ]] && : > "$REVIEW_SENTINEL"
    if [[ -n "${ROLLUP_JSON:-}" ]]; then
      expr="" prev=""
      for a in "$@"; do [[ "$prev" == "--jq" ]] && expr="$a"; prev="$a"; done
      printf '%s' "$ROLLUP_JSON" | jq -rc "$expr"
    else
      printf '%s|%s|%s\n' "${PR_AUTHOR:-}" "${REVIEW_CONCLUSIONS:-}" "${NON_REVIEW_SUCCESSES-1}"
    fi
    exit "${REVIEW_EC:-0}" ;;
  *"--json mergeStateStatus"*)
    printf '%s|%s|%s\n' "${MERGE_STATE:-CLEAN}" "${HEAD_DATE:-}" "${HEAD_AUTHOR-dependabot[bot]}" ;;
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

# --- pending: no checks registered yet (exit 1 + the gh "no checks reported"
# stderr signature) is CI-hasn't-started, not CI-failed ----------------------
check "exit 1 + 'no checks reported' stderr → pending" "pending" "$(CHECKS_NO_CHECKS=1 run 100)"

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

# --- the four verdict states are FOUR tokens, not two (issue #1097) ---------
# `awaiting-review` used to swallow missing, stale AND fresh-non-LGTM verdicts,
# so watch-pr.sh (whose in-flight set contains `awaiting-review`) could never
# wake on the one Gate 4 outcome that needs the orchestrator soonest: a fresh
# CHANGES_REQUESTED/COMMENTS. A fresh non-LGTM verdict is ACTIONABLE (Step 2,
# address-feedback) and gets its own token; missing and stale stay "wait".
check "green + CLEAN + fresh non-LGTM → changes-requested" "changes-requested" \
  "$(CHECKS_EC=0 MERGE_STATE=CLEAN HEAD_DATE=$H VERDICT="$FRESH|false" run 100)"

# A STALE non-LGTM reviewed code that is no longer HEAD — the re-review is still
# owed, so this is a wait, never a dispatch.
check "green + CLEAN + STALE non-LGTM → awaiting-review" "awaiting-review" \
  "$(CHECKS_EC=0 MERGE_STATE=CLEAN HEAD_DATE=$H VERDICT="$STALE|false" run 100)"

# Fail CLOSED on a malformed verdict answer: the second field must be exactly
# jq's `true`/`false`. Garbage there must degrade to `awaiting-review` (wait),
# never to `changes-requested` (dispatch a fix worker on an unreadable answer).
check "malformed verdict flag → awaiting-review, never changes-requested" "awaiting-review" \
  "$(CHECKS_EC=0 MERGE_STATE=CLEAN HEAD_DATE=$H VERDICT="$FRESH|garbage" run 100)"

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
  # LGTM — the exact false-positive a whole-body match would cause. It is a real,
  # fresh, non-LGTM verdict, so it classifies as actionable `changes-requested`.
  check "real CHANGES_REQUESTED w/ 'LGTM' in prose → changes-requested" "changes-requested" \
    "$(CHECKS_EC=0 MERGE_STATE=CLEAN HEAD_DATE=$H \
       COMMENTS_JSON="$(cj '{"createdAt":"'"$FRESH"'","body":"Not ready for LGTM yet.\n\n**Verdict:** CHANGES_REQUESTED\n"}')" \
       run 100)"

  # The other non-LGTM verdict the reviewer posts. Observed live on PR #1095:
  # a fresh `## Verdict: COMMENTS` + fully green CI sat unnoticed for the
  # watcher's whole timeout because it classified as in-flight `awaiting-review`.
  check "real ## Verdict: COMMENTS (fresh) → changes-requested" "changes-requested" \
    "$(CHECKS_EC=0 MERGE_STATE=CLEAN HEAD_DATE=$H \
       COMMENTS_JSON="$(cj '{"createdAt":"'"$FRESH"'","body":"## Summary\nnits\n\n## Verdict: COMMENTS\n"}')" \
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

# --- opt-out: a human hold beats every other signal ------------------------
# `do-not-auto-merge` is the kill switch a human reaches for when a lane is green
# but must NOT be merged (a revert in flight, a coordinated release, a bump that
# needs a manual smoke test). It is read from the PR's own labels AND from the
# issue the PR closes, and it is checked FIRST — a hold applied while CI is still
# running has to land before the loop can act on the green that follows.
OPTOUT="do-not-auto-merge"

check "opt-out label on the PR → optout" "optout" \
  "$(CHECKS_EC=0 MERGE_STATE=CLEAN HEAD_DATE=$H VERDICT="$FRESH|true" BEHIND_BY=0 \
     PR_LABELS="dependencies,$OPTOUT" run 100)"

# The hold usually lives on the ISSUE (that is where the human is arguing about
# it), not on the bot-authored PR — so the PR's closes-link must be followed.
check "opt-out label on the linked issue → optout" "optout" \
  "$(CHECKS_EC=0 MERGE_STATE=CLEAN HEAD_DATE=$H VERDICT="$FRESH|true" BEHIND_BY=0 \
     PR_BODY="Bumps ruff to 0.16.0. Closes #944" ISSUE_LABELS="dependencies,$OPTOUT" \
     run 100)"

# Order matters: if CI were consulted first, a lane that is pending now and green
# in ten minutes would be merged by the next wake without ever reading the hold.
check "opt-out short-circuits pending CI → optout" "optout" \
  "$(CHECKS_EC=8 PR_LABELS="dependencies,$OPTOUT" run 100)"

# The mirror image: ordinary Dependabot labels must not be mistaken for a hold,
# or every dependency bump silently stops merging.
check "dependabot labels without the opt-out → ready" "ready" \
  "$(CHECKS_EC=0 MERGE_STATE=CLEAN HEAD_DATE=$H VERDICT="$FRESH|true" BEHIND_BY=0 \
     PR_LABELS="dependencies,python" run 100)"

# A substring match would park `do-not-auto-merge-after-review` — a label that
# means the OPPOSITE (merge it, just not yet automatically-before-review).
check "label merely containing the opt-out name → ready" "ready" \
  "$(CHECKS_EC=0 MERGE_STATE=CLEAN HEAD_DATE=$H VERDICT="$FRESH|true" BEHIND_BY=0 \
     PR_LABELS="dependencies,$OPTOUT-after-review" run 100)"

check "linked issue without the opt-out label → ready" "ready" \
  "$(CHECKS_EC=0 MERGE_STATE=CLEAN HEAD_DATE=$H VERDICT="$FRESH|true" BEHIND_BY=0 \
     PR_BODY="Closes #944" ISSUE_LABELS="dependencies,bug" run 100)"

# UNDETERMINABLE hold ⇒ exit 2 with NO token. Printing `ready` (or any token) on a
# failed lookup would merge straight through a hold nobody could read; printing a
# refusal token would be a lie about what was checked. The caller must see the
# tooling error, so: non-zero exit, empty stdout. Three lookups, three cases.
rc=0
out="$(CHECKS_EC=0 MERGE_STATE=CLEAN HEAD_DATE=$H VERDICT="$FRESH|true" BEHIND_BY=0 \
       PR_LABELS_EC=1 run 100)" || rc=$?
check "PR label lookup failure exits 2" "2" "$rc"
check "PR label lookup failure prints nothing" "" "$out"

rc=0
out="$(CHECKS_EC=0 MERGE_STATE=CLEAN HEAD_DATE=$H VERDICT="$FRESH|true" BEHIND_BY=0 \
       PR_BODY_EC=1 run 100)" || rc=$?
check "PR body lookup failure exits 2" "2" "$rc"
check "PR body lookup failure prints nothing" "" "$out"

rc=0
out="$(CHECKS_EC=0 MERGE_STATE=CLEAN HEAD_DATE=$H VERDICT="$FRESH|true" BEHIND_BY=0 \
       PR_BODY="Closes #944" ISSUE_LABELS_EC=1 run 100)" || rc=$?
check "linked-issue label lookup failure exits 2" "2" "$rc"
check "linked-issue label lookup failure prints nothing" "" "$out"

# Control for the three above: with all three lookups SUCCEEDING and no hold, the
# same lane must still be a plain, exit-0 `ready` — so the exit-2 cases above are
# provably about the failure, not about the opt-out check existing at all.
rc=0
out="$(CHECKS_EC=0 MERGE_STATE=CLEAN HEAD_DATE=$H VERDICT="$FRESH|true" BEHIND_BY=0 \
       PR_LABELS="dependencies" PR_BODY="Closes #944" ISSUE_LABELS="dependencies" \
       run 100)" || rc=$?
check "all three opt-out lookups clean → ready" "ready" "$out"
check "all three opt-out lookups clean → exit 0" "0" "$rc"

# LAST link wins. A Dependabot body embeds the DEPENDENCY's changelog, which is
# full of `Fixes #456` / `Resolves #789` referring to the UPSTREAM repo's issues.
# A first-match parser reads one of those as "the issue this PR closes" and then
# checks labels on a completely unrelated local issue number.
CHANGELOG_BODY="$(printf '%s\n' \
  'Bumps ruff from 0.15.0 to 0.16.0.' \
  '' \
  '<details><summary>Changelog</summary>' \
  '* Fixes #456 in the upstream tool' \
  '* Resolves #789 in the upstream tool' \
  '</details>' \
  '' \
  'Closes #944')"

check "changelog noise: hold on the LAST-linked issue → optout" "optout" \
  "$(CHECKS_EC=0 MERGE_STATE=CLEAN HEAD_DATE=$H VERDICT="$FRESH|true" BEHIND_BY=0 \
     PR_BODY="$CHANGELOG_BODY" ISSUE_LABELS_FOR=944 ISSUE_LABELS="dependencies,$OPTOUT" \
     run 100)"

# The twin: the hold sits on the upstream-changelog number instead. Serving it for
# #456 only means a first-match parser prints `optout` here — and that FALSE hold
# would silently freeze the lane forever.
check "changelog noise: hold on an upstream #456 is not ours → ready" "ready" \
  "$(CHECKS_EC=0 MERGE_STATE=CLEAN HEAD_DATE=$H VERDICT="$FRESH|true" BEHIND_BY=0 \
     PR_BODY="$CHANGELOG_BODY" ISSUE_LABELS_FOR=456 ISSUE_LABELS="$OPTOUT" \
     run 100)"

# --- freshness: mergeStateStatus is NOT a freshness signal ------------------
# Live PR #943 reports UNSTABLE/MERGEABLE while sitting exactly 22 commits behind
# main: GitHub only says BEHIND when the base branch requires strict up-to-date
# merging. So `ready` must ask the compare API for `behind_by` directly, or the
# loop merges branches that never ran against current main.
check "green + CLEAN + fresh LGTM but 22 behind, overlapping file → behind" "behind" \
  "$(CHECKS_EC=0 MERGE_STATE=CLEAN HEAD_DATE=$H VERDICT="$FRESH|true" BEHIND_BY=22 \
     THEIR_FILES="creek/vault/writer.py" OUR_FILES="creek/vault/writer.py" run 100)"

check "behind_by 0 → ready" "ready" \
  "$(CHECKS_EC=0 MERGE_STATE=CLEAN HEAD_DATE=$H VERDICT="$FRESH|true" BEHIND_BY=0 run 100)"

# --- behind ≠ stale: the #1137 regression ----------------------------------
# Requiring `behind_by == 0` outright made every merge to `main` invalidate every
# OTHER open lane: each paid a sync, a ~14-minute CI round and a full re-review,
# and because that window is as long as the gap between merges, lanes went stale
# again WHILE re-proving themselves. Two bug fixes in different modules cannot
# turn each other red, so being behind is stale only for a REASON.
check "behind but disjoint, inert files → ready" "ready" \
  "$(CHECKS_EC=0 MERGE_STATE=CLEAN HEAD_DATE=$H VERDICT="$FRESH|true" BEHIND_BY=22 \
     THEIR_FILES="creek/classify/privacy.py,tests/test_privacy.py" \
     OUR_FILES="creek/vault/writer.py,tests/test_writer.py" run 100)"

# One shared file IS a semantic interaction, even buried in a longer list.
check "behind + one overlapping file among many → behind" "behind" \
  "$(CHECKS_EC=0 MERGE_STATE=CLEAN HEAD_DATE=$H VERDICT="$FRESH|true" BEHIND_BY=3 \
     THEIR_FILES="creek/a.py,creek/shared.py,creek/b.py" \
     OUR_FILES="creek/c.py,creek/d.py,creek/shared.py" run 100)"

# A prefix match would read `creek/vault.py` as overlapping `creek/vault_index.py`
# and re-impose the very serialization this change removes.
check "behind + merely prefix-similar paths → ready" "ready" \
  "$(CHECKS_EC=0 MERGE_STATE=CLEAN HEAD_DATE=$H VERDICT="$FRESH|true" BEHIND_BY=2 \
     THEIR_FILES="creek/vault.py" OUR_FILES="creek/vault_index.py" run 100)"

# --- the risk surface: what #1022 was actually right about ------------------
# PR #943 was a `ruff` bump 22 commits behind, and a bump 17 behind carrying a
# ruff major produced 144 lint errors against the then-current tree. A change to
# how the tree is built/linted/typed/tested invalidates EVERY branch's green,
# overlap or not — so each of these must still force a sync.
for risky in "creek-tools/uv.lock" "creek-tools/pyproject.toml" \
             "crawdad/requirements-dev.txt" ".pre-commit-config.yaml" \
             "creek-tools/tests/conftest.py" ".github/workflows/ci.yml" \
             "creek-tools/scripts/check-all.sh"; do
  check "risk surface on main ($risky) → behind" "behind" \
    "$(CHECKS_EC=0 MERGE_STATE=CLEAN HEAD_DATE=$H VERDICT="$FRESH|true" BEHIND_BY=4 \
       THEIR_FILES="creek/unrelated.py,$risky" OUR_FILES="creek/mine.py" run 100)"
done

# OUR side of the risk surface counts too, and the reason is sharper: a branch
# that changes the tooling has proved it only against the tree at its merge base,
# so everything main landed after that is code the new tooling never ran over.
check "risk surface on OUR side, inert main → behind" "behind" \
  "$(CHECKS_EC=0 MERGE_STATE=CLEAN HEAD_DATE=$H VERDICT="$FRESH|true" BEHIND_BY=4 \
     THEIR_FILES="creek/unrelated.py" OUR_FILES="creek/mine.py,creek-tools/uv.lock" run 100)"

# The twin: a path that merely LOOKS like the risk surface is not it. Without
# this, `docs/pyproject.toml.md` or `creek/scripts/helper.py` would silently
# re-serialize the fleet.
check "risk-surface look-alikes are inert → ready" "ready" \
  "$(CHECKS_EC=0 MERGE_STATE=CLEAN HEAD_DATE=$H VERDICT="$FRESH|true" BEHIND_BY=4 \
     THEIR_FILES="docs/pyproject.toml.md,creek/scripts/helper.py,docs/workflows/ci.yml" \
     OUR_FILES="creek/mine.py" run 100)"

# --- fail closed on every unusable answer ----------------------------------
# The file listings decide whether to SKIP a sync, so an answer we cannot trust
# has to read as "sync anyway" — the remedy is always safe, a false `ready` is not.
check "main-side file listing errors → behind" "behind" \
  "$(CHECKS_EC=0 MERGE_STATE=CLEAN HEAD_DATE=$H VERDICT="$FRESH|true" BEHIND_BY=5 \
     THEIR_FILES="creek/a.py" THEIR_FILES_EC=1 OUR_FILES="creek/b.py" run 100)"

check "branch-side file listing errors → behind" "behind" \
  "$(CHECKS_EC=0 MERGE_STATE=CLEAN HEAD_DATE=$H VERDICT="$FRESH|true" BEHIND_BY=5 \
     THEIR_FILES="creek/a.py" OUR_FILES="creek/b.py" OUR_FILES_EC=1 run 100)"

# GitHub caps `.files` at 300. AT the cap the list is truncated, so "disjoint" is
# an answer the data cannot support.
TRUNCATED="$(seq 1 300 | sed 's|^|creek/f|; s|$|.py|' | paste -sd, -)"
check "300-file (capped) main listing → behind" "behind" \
  "$(CHECKS_EC=0 MERGE_STATE=CLEAN HEAD_DATE=$H VERDICT="$FRESH|true" BEHIND_BY=6 \
     THEIR_FILES="$TRUNCATED" OUR_FILES="creek/mine.py" run 100)"

# 299 is under the cap and therefore a complete, usable list — the non-vacuity
# twin proving the check above is about truncation, not about list length.
UNDER_CAP="$(seq 1 299 | sed 's|^|creek/f|; s|$|.py|' | paste -sd, -)"
check "299-file (uncapped) main listing → ready" "ready" \
  "$(CHECKS_EC=0 MERGE_STATE=CLEAN HEAD_DATE=$H VERDICT="$FRESH|true" BEHIND_BY=6 \
     THEIR_FILES="$UNDER_CAP" OUR_FILES="creek/mine.py" run 100)"

# The merge base comes from the same call as behind_by and builds the main-side
# compare range; a malformed one would compare against a garbage ref.
check "malformed merge base → behind" "behind" \
  "$(CHECKS_EC=0 MERGE_STATE=CLEAN HEAD_DATE=$H VERDICT="$FRESH|true" BEHIND_BY=7 \
     MERGE_BASE="not-a-sha" THEIR_FILES="creek/a.py" OUR_FILES="creek/b.py" run 100)"

check "empty merge base → behind" "behind" \
  "$(CHECKS_EC=0 MERGE_STATE=CLEAN HEAD_DATE=$H VERDICT="$FRESH|true" BEHIND_BY=7 \
     MERGE_BASE="" THEIR_FILES="creek/a.py" OUR_FILES="creek/b.py" run 100)"

# Fail CLOSED. A compare call that errors (rate limit, 404 on a force-pushed OID)
# must not inherit the happy answer that happens to be on stdout.
check "compare API error → behind" "behind" \
  "$(CHECKS_EC=0 MERGE_STATE=CLEAN HEAD_DATE=$H VERDICT="$FRESH|true" \
     BEHIND_BY=0 COMPARE_EC=1 run 100)"

# An EMPTY answer is the dangerous one: `[[ "" -eq 0 ]]` is TRUE in bash, so a
# naive numeric test reads "no answer" as "up to date".
check "empty behind_by answer → behind" "behind" \
  "$(CHECKS_EC=0 MERGE_STATE=CLEAN HEAD_DATE=$H VERDICT="$FRESH|true" BEHIND_BY='' run 100)"

# `--jq .behind_by` prints the literal `null` when the field is absent.
check "null behind_by answer → behind" "behind" \
  "$(CHECKS_EC=0 MERGE_STATE=CLEAN HEAD_DATE=$H VERDICT="$FRESH|true" BEHIND_BY=null run 100)"

# The new probe ADDS to the CLEAN requirement, it does not replace it: a BEHIND
# mergeStateStatus still blocks even when compare says 0.
check "BEHIND merge state with behind_by 0 → behind" "behind" \
  "$(CHECKS_EC=0 MERGE_STATE=BEHIND HEAD_DATE=$H VERDICT="$FRESH|true" BEHIND_BY=0 run 100)"

# The compare INPUTS are parsed defensively too. `<base>|<headOid>` is split on
# the LAST '|' (a branch name may legally contain one; a SHA may not) — but a
# separator-free answer would leave base and headOid BOTH equal to the whole
# string, and a base ref that looks like a short SHA would then be compared
# against itself: `behind_by: 0`, a false `ready`. `c0ffee1` is exactly such a
# name, so this case fails on any implementation that splits before checking.
check "separator-free compare answer that looks like a SHA → behind" "behind" \
  "$(CHECKS_EC=0 MERGE_STATE=CLEAN HEAD_DATE=$H VERDICT="$FRESH|true" \
     BASE_LINE_RAW="c0ffee1" BEHIND_BY=0 run 100)"

# An empty answer (gh printing nothing while still exiting 0) is the same class.
check "empty compare answer → behind" "behind" \
  "$(CHECKS_EC=0 MERGE_STATE=CLEAN HEAD_DATE=$H VERDICT="$FRESH|true" \
     BASE_LINE_RAW="" BEHIND_BY=0 run 100)"

# The legal twin: refusing every unusual base name would break a real one, so a
# genuinely pipe-named base ref must still resolve and still read `ready`.
check "pipe-named base ref still resolves → ready" "ready" \
  "$(CHECKS_EC=0 MERGE_STATE=CLEAN HEAD_DATE=$H VERDICT="$FRESH|true" \
     BASE_REF='release|v2' HEAD_OID=deadbee BEHIND_BY=0 run 100)"

# --- freshness probe is LAZY -----------------------------------------------
# The compare call is an extra API request per lane per wake. It must fire ONLY on
# the path that would otherwise print `ready`; every lane below has already
# decided, so the probe is pure waste (and, on a rate-limited token, the thing
# that makes the whole tick fail). BEHIND_BY=22 stays set throughout to prove the
# printed token came from the EARLIER check, not from the freshness probe.
# Each lane gets its OWN sentinel path: a shared one would let an earlier lane's
# probe satisfy a later lane's assertion. `|| tok="exit-$?"` keeps a non-zero exit
# from aborting the whole suite under `set -e` — it surfaces as a failed check.
S_PENDING="$WORK/probe-pending"
tok="$(CHECKS_EC=8 BEHIND_BY=22 COMPARE_SENTINEL="$S_PENDING" run 100)" || tok="exit-$?"
check "lazy compare: pending lane token" "pending" "$tok"
probed "lazy compare: pending lane does not probe" "no" "$S_PENDING"

S_CIFAIL="$WORK/probe-cifail"
tok="$(CHECKS_EC=1 BEHIND_BY=22 COMPARE_SENTINEL="$S_CIFAIL" run 100)" || tok="exit-$?"
check "lazy compare: ci-failed lane token" "ci-failed" "$tok"
probed "lazy compare: ci-failed lane does not probe" "no" "$S_CIFAIL"

S_STALE="$WORK/probe-stale"
tok="$(CHECKS_EC=0 MERGE_STATE=CLEAN HEAD_DATE=$H VERDICT="$STALE|true" \
       BEHIND_BY=22 COMPARE_SENTINEL="$S_STALE" run 100)" || tok="exit-$?"
check "lazy compare: stale-verdict lane token" "awaiting-review" "$tok"
probed "lazy compare: stale-verdict lane does not probe" "no" "$S_STALE"

S_CR="$WORK/probe-changes-requested"
tok="$(CHECKS_EC=0 MERGE_STATE=CLEAN HEAD_DATE=$H VERDICT="$FRESH|false" \
       BEHIND_BY=22 COMPARE_SENTINEL="$S_CR" run 100)" || tok="exit-$?"
check "lazy compare: changes-requested lane token" "changes-requested" "$tok"
probed "lazy compare: changes-requested lane does not probe" "no" "$S_CR"

S_READY="$WORK/probe-ready"
tok="$(CHECKS_EC=0 MERGE_STATE=CLEAN HEAD_DATE=$H VERDICT="$FRESH|true" \
       BEHIND_BY=0 COMPARE_SENTINEL="$S_READY" run 100)" || tok="exit-$?"
check "lazy compare: would-be-ready lane token" "ready" "$tok"
probed "lazy compare: would-be-ready lane DOES probe" "yes" "$S_READY"

# The file listings are lazy INSIDE the compare probe as well: `behind_by == 0`
# is the overwhelmingly common answer and must still cost exactly ONE request,
# or the fix for #1137 would tax every already-current lane on every wake.
S_F_CURRENT="$WORK/probe-files-current"
tok="$(CHECKS_EC=0 MERGE_STATE=CLEAN HEAD_DATE=$H VERDICT="$FRESH|true" \
       BEHIND_BY=0 FILES_SENTINEL="$S_F_CURRENT" run 100)" || tok="exit-$?"
check "lazy files: current lane token" "ready" "$tok"
probed "lazy files: current lane does not list files" "no" "$S_F_CURRENT"

S_F_BEHIND="$WORK/probe-files-behind"
tok="$(CHECKS_EC=0 MERGE_STATE=CLEAN HEAD_DATE=$H VERDICT="$FRESH|true" BEHIND_BY=9 \
       THEIR_FILES="creek/a.py" OUR_FILES="creek/b.py" \
       FILES_SENTINEL="$S_F_BEHIND" run 100)" || tok="exit-$?"
check "lazy files: behind lane token" "ready" "$tok"
probed "lazy files: behind lane DOES list files" "yes" "$S_F_BEHIND"

# And a lane behind a RISK-SURFACE change never pays for the branch-side listing:
# the main-side answer already decided it.
S_F_RISK="$WORK/probe-files-risk"
tok="$(CHECKS_EC=0 MERGE_STATE=CLEAN HEAD_DATE=$H VERDICT="$FRESH|true" BEHIND_BY=9 \
       THEIR_FILES="creek-tools/uv.lock" OUR_FILES="creek/b.py" OUR_FILES_EC=1 \
       FILES_SENTINEL="$S_F_RISK" run 100)" || tok="exit-$?"
check "lazy files: risk-surface lane short-circuits" "behind" "$tok"

S_OPTOUT="$WORK/probe-optout"
tok="$(CHECKS_EC=0 MERGE_STATE=CLEAN HEAD_DATE=$H VERDICT="$FRESH|true" \
       BEHIND_BY=22 PR_LABELS="$OPTOUT" COMPARE_SENTINEL="$S_OPTOUT" run 100)" || tok="exit-$?"
check "lazy compare: opt-out lane token" "optout" "$tok"
probed "lazy compare: opt-out lane does not probe" "no" "$S_OPTOUT"

# --- main health: the #1157 relaxation's PRECONDITION (issue #1159) ---------
# #1157 let a lane that is behind `main` merge anyway whenever the two
# changesets provably cannot interact. Its whole justification is one sentence
# in pr-ready.sh's header (lines 129-133): "What backstops the residual risk is
# the full CI run on `push: main` — every squash-merge re-proves the merged
# result." Nothing in the loop had ever read that run's conclusion, so the
# backstop was an assumption. It is now a check, and it is deliberately a
# precondition of the RELAXATION, not a new gate on merging: only a lane that is
# about to skip a sync has to prove the backstop is alive.
#
# The probe sits inside `branch_is_current`, AFTER the `behind_by == 0` fast
# path and AFTER the merge-base validation, and BEFORE `main_changes_are_inert`.
# Anything other than `green` — including an empty answer or a missing helper —
# holds the lane as `main-not-green`.
#
# THE ANTI-MASKING PROOF: because the stub defaults MAIN_HEALTH to `green`,
# someone could delete the probe from pr-ready.sh entirely and every
# pre-existing test in this file would still pass. Three assertions below make
# that impossible, and they only work TOGETHER:
#   (1) MAIN_HEALTH=red → `main-not-green`   — the answer is actually consumed;
#   (2) the sentinel is PRESENT on that lane — the call is actually made;
#   (3) the sentinel is ABSENT on a BEHIND_BY=0 lane — the laziness holds, and
#       the deadlock exception below is real rather than an accident.
# Delete the probe and (1) and (2) fail; make the probe unconditional and (3)
# fails. Do not remove any one of the three believing the others cover it.

# THE RED CASE. This is the #1137 behind-but-inert lane — the one #1157 taught
# the loop to merge — with `main` itself broken. Merging it would stack a second
# unvalidated change on top of a tree that is already red, and the loop would
# then read the resulting CI failure as THIS lane's fault.
check "behind + inert files but main CI RED → main-not-green" "main-not-green" \
  "$(CHECKS_EC=0 MERGE_STATE=CLEAN HEAD_DATE=$H VERDICT="$FRESH|true" BEHIND_BY=22 \
     THEIR_FILES="creek/classify/privacy.py" OUR_FILES="creek/vault/writer.py" \
     MAIN_HEALTH=red run 100)"

# Non-vacuity, and the guard that #1157 was not quietly re-tightened: the SAME
# lane with a healthy `main` still merges. If this ever flips to `behind` or
# `main-not-green`, the serialization #1137 measured (CI runs per PR 1.00 →
# 1.61, p90 latency 15 → 104 minutes) is back.
check "behind + inert files, main CI green → ready" "ready" \
  "$(CHECKS_EC=0 MERGE_STATE=CLEAN HEAD_DATE=$H VERDICT="$FRESH|true" BEHIND_BY=22 \
     THEIR_FILES="creek/classify/privacy.py" OUR_FILES="creek/vault/writer.py" \
     MAIN_HEALTH=green run 100)"

# THE DEADLOCK PIN. A lane that is NOT behind merges even while `main` is red,
# and this must stay true forever, so a future "tighten it" refactor cannot
# freeze the whole loop the first time somebody breaks `main`.
#
# Why it is safe, precisely: `behind_by == 0` means `main`'s HEAD is already an
# ancestor of this branch's head. `.github/workflows/ci.yml` carries NO `paths:`
# filter and runs the IDENTICAL job matrix on `push` and `pull_request`, and its
# `actions/checkout@v7` has no `ref:` override — so this PR's CI ran on
# `refs/pull/N/merge`, a tree that already contains whatever broke `main`. Its
# green is therefore positive proof that the breakage is either absent from the
# merged result or fixed by this very branch.
#
# Why it matters: that is exactly the shape of the PR that FIXES `main`. Without
# this exception the remedy for a red `main` would itself be blocked by the red
# `main`, and the loop would need a bypass label — one more thing to get wrong,
# and one more thing that can be left switched on. Here the loop cannot deadlock
# by construction.
check "behind_by 0 with main CI RED → ready (the deadlock pin)" "ready" \
  "$(CHECKS_EC=0 MERGE_STATE=CLEAN HEAD_DATE=$H VERDICT="$FRESH|true" BEHIND_BY=0 \
     MAIN_HEALTH=red run 100)"

# Fail CLOSED on everything that is not a green answer. `pending` is the common
# one — `main` CI lags each merge by ~14 minutes — and it is still not evidence
# that the backstop caught anything, so a lane that wants to SKIP a sync waits
# one wake rather than merging on a maybe.
check "behind + inert but main CI PENDING → main-not-green" "main-not-green" \
  "$(CHECKS_EC=0 MERGE_STATE=CLEAN HEAD_DATE=$H VERDICT="$FRESH|true" BEHIND_BY=22 \
     THEIR_FILES="creek/classify/privacy.py" OUR_FILES="creek/vault/writer.py" \
     MAIN_HEALTH=pending run 100)"

check "behind + inert but main health UNKNOWN → main-not-green" "main-not-green" \
  "$(CHECKS_EC=0 MERGE_STATE=CLEAN HEAD_DATE=$H VERDICT="$FRESH|true" BEHIND_BY=22 \
     THEIR_FILES="creek/classify/privacy.py" OUR_FILES="creek/vault/writer.py" \
     MAIN_HEALTH=unknown run 100)"

# A gh failure inside the sibling must not kill pr-ready.sh: the call sits under
# `set -e`, so an unguarded invocation would abort the script mid-classification
# and the orchestrator would see an empty answer with a non-zero exit — which its
# contract defines as a TOOLING error, dispatching nothing and logging nothing
# useful. MAIN_HEALTH=green alongside EC=1 is the dangerous shape: the happy
# answer is already on stdout when the call fails.
rc=0
out="$(CHECKS_EC=0 MERGE_STATE=CLEAN HEAD_DATE=$H VERDICT="$FRESH|true" BEHIND_BY=22 \
       THEIR_FILES="creek/classify/privacy.py" OUR_FILES="creek/vault/writer.py" \
       MAIN_HEALTH=green MAIN_HEALTH_EC=1 run 100)" || rc=$?
check "main-health lookup failure → main-not-green" "main-not-green" "$out"
check "main-health lookup failure still exits 0" "0" "$rc"

# ORDERING — never sync INTO the breakage. A lane behind a RISK-SURFACE change
# would normally print `behind`, whose remedy is `fleet.sh sync`. With `main`
# red that remedy is actively harmful: the sync pulls the breakage into the
# lane, burns a ~14-minute CI round, and turns the lane's OWN CI red — which
# pr-ready.sh then classifies `ci-failed`, dispatching a fix worker onto a
# failure the lane never caused. So main-health is consulted BEFORE the file
# comparison, and `main-not-green` (wait) outranks `behind` (act).
check "behind a risk-surface change with main CI RED → main-not-green, not behind" \
  "main-not-green" \
  "$(CHECKS_EC=0 MERGE_STATE=CLEAN HEAD_DATE=$H VERDICT="$FRESH|true" BEHIND_BY=22 \
     THEIR_FILES="creek-tools/uv.lock" OUR_FILES="creek/mine.py" \
     MAIN_HEALTH=red run 100)"

# The twin: with `main` healthy the same lane is still `behind` — the ordering
# change must not have swallowed #1157's risk-surface rule.
check "behind a risk-surface change with main CI green → behind" "behind" \
  "$(CHECKS_EC=0 MERGE_STATE=CLEAN HEAD_DATE=$H VERDICT="$FRESH|true" BEHIND_BY=22 \
     THEIR_FILES="creek-tools/uv.lock" OUR_FILES="creek/mine.py" \
     MAIN_HEALTH=green run 100)"

# --- REAL jq: exercise main-health.sh's production run-list expression ------
# The scalar arm above cannot catch a `--jq` that drops a field or trips over
# the two type surprises in this payload: `databaseId` is a NUMBER and
# `conclusion` is JSON null while a run is in flight. Feeding raw payloads
# through the REAL sibling proves the two scripts agree end to end — including
# on the token STRING, which a fake sibling would let drift.
if command -v jq >/dev/null 2>&1; then
  MH_SUCCESS='{"status":"completed","conclusion":"success","headSha":"abc1234","databaseId":11,"url":"https://x/11"}'
  MH_FAILURE='{"status":"completed","conclusion":"failure","headSha":"dead1234","databaseId":12,"url":"https://x/12"}'
  MH_FLIGHT='{"status":"in_progress","conclusion":null,"headSha":"feed1234","databaseId":13,"url":"https://x/13"}'

  check "real run-list payload, main failing → main-not-green" "main-not-green" \
    "$(CHECKS_EC=0 MERGE_STATE=CLEAN HEAD_DATE=$H VERDICT="$FRESH|true" BEHIND_BY=22 \
       THEIR_FILES="creek/classify/privacy.py" OUR_FILES="creek/vault/writer.py" \
       MAIN_RUNS_JSON="[$MH_FAILURE,$MH_SUCCESS]" run 100)"

  # A run in flight over an older success is the STEADY STATE of a busy fleet
  # (main CI lags every merge by ~14 minutes). It must read green, or this gate
  # reintroduces the serialization #1138 removed.
  check "real run-list payload, in flight over a success → ready" "ready" \
    "$(CHECKS_EC=0 MERGE_STATE=CLEAN HEAD_DATE=$H VERDICT="$FRESH|true" BEHIND_BY=22 \
       THEIR_FILES="creek/classify/privacy.py" OUR_FILES="creek/vault/writer.py" \
       MAIN_RUNS_JSON="[$MH_FLIGHT,$MH_SUCCESS]" run 100)"

  check "real run-list payload, empty window → main-not-green" "main-not-green" \
    "$(CHECKS_EC=0 MERGE_STATE=CLEAN HEAD_DATE=$H VERDICT="$FRESH|true" BEHIND_BY=22 \
       THEIR_FILES="creek/classify/privacy.py" OUR_FILES="creek/vault/writer.py" \
       MAIN_RUNS_JSON='[]' run 100)"
else
  echo "  skip - real-jq main-health payload cases (jq not installed)"
fi

# --- the sibling seam: a helper that is not there holds the lane ------------
# pr-ready.sh resolves main-health.sh by its own `dirname`, exactly as
# watch-pr.sh resolves pr-ready.sh (test_watch_pr.sh:53). A copy of the script
# in a directory with no sibling IS that seam. A partial checkout, a bad
# packaging change, or a rename must never read as "main is fine".
NOSIB="$WORK/nosibling"
mkdir -p "$NOSIB"
cp "$READY" "$NOSIB/pr-ready.sh"
chmod +x "$NOSIB/pr-ready.sh"
run_nosib() { PATH="$BIN:$PATH" "$NOSIB/pr-ready.sh" "$@" 2>/dev/null; }

tok="$(CHECKS_EC=0 MERGE_STATE=CLEAN HEAD_DATE=$H VERDICT="$FRESH|true" BEHIND_BY=22 \
       THEIR_FILES="creek/classify/privacy.py" OUR_FILES="creek/vault/writer.py" \
       run_nosib 100)" || tok="exit-$?"
no_merge_token "missing main-health.sh sibling is never mergeable" "$tok"

# And a helper that exists but cannot be executed (a dropped exec bit — the
# exact failure test_exec_bits.sh guards, and the one #1092 actually shipped).
# The planted file WOULD answer `green` if it ran, so an implementation that
# `bash`es the helper instead of checking it is executable fails here.
NOEXEC="$WORK/noexec"
mkdir -p "$NOEXEC"
cp "$READY" "$NOEXEC/pr-ready.sh"
chmod +x "$NOEXEC/pr-ready.sh"
printf '#!/usr/bin/env bash\necho green\n' > "$NOEXEC/main-health.sh"
chmod 644 "$NOEXEC/main-health.sh"
run_noexec() { PATH="$BIN:$PATH" "$NOEXEC/pr-ready.sh" "$@" 2>/dev/null; }

tok="$(CHECKS_EC=0 MERGE_STATE=CLEAN HEAD_DATE=$H VERDICT="$FRESH|true" BEHIND_BY=22 \
       THEIR_FILES="creek/classify/privacy.py" OUR_FILES="creek/vault/writer.py" \
       run_noexec 100)" || tok="exit-$?"
no_merge_token "non-executable main-health.sh sibling is never mergeable" "$tok"

# --- main-health probe is LAZY ---------------------------------------------
# Same rate-limit argument as the compare and review-gate probes: this is one
# more API request per lane per wake, and it can only change the outcome for a
# lane that is about to USE the #1157 relaxation. Every lane below has already
# decided. Each gets its OWN sentinel path — a shared one would let an earlier
# lane's probe satisfy a later lane's assertion.
M_OPTOUT="$WORK/main-health-optout"
tok="$(CHECKS_EC=0 MERGE_STATE=CLEAN HEAD_DATE=$H VERDICT="$FRESH|true" BEHIND_BY=22 \
       PR_LABELS="$OPTOUT" MAIN_HEALTH_SENTINEL="$M_OPTOUT" run 100)" || tok="exit-$?"
check "lazy main-health: opt-out lane token" "optout" "$tok"
probed "lazy main-health: opt-out lane does not probe" "no" "$M_OPTOUT"

M_PENDING="$WORK/main-health-pending"
tok="$(CHECKS_EC=8 BEHIND_BY=22 MAIN_HEALTH_SENTINEL="$M_PENDING" run 100)" || tok="exit-$?"
check "lazy main-health: pending lane token" "pending" "$tok"
probed "lazy main-health: pending lane does not probe" "no" "$M_PENDING"

M_CIFAIL="$WORK/main-health-cifail"
tok="$(CHECKS_EC=1 BEHIND_BY=22 MAIN_HEALTH_SENTINEL="$M_CIFAIL" run 100)" || tok="exit-$?"
check "lazy main-health: ci-failed lane token" "ci-failed" "$tok"
probed "lazy main-health: ci-failed lane does not probe" "no" "$M_CIFAIL"

M_CR="$WORK/main-health-changes-requested"
tok="$(CHECKS_EC=0 MERGE_STATE=CLEAN HEAD_DATE=$H VERDICT="$FRESH|false" BEHIND_BY=22 \
       MAIN_HEALTH_SENTINEL="$M_CR" run 100)" || tok="exit-$?"
check "lazy main-health: changes-requested lane token" "changes-requested" "$tok"
probed "lazy main-health: changes-requested lane does not probe" "no" "$M_CR"

M_STALE="$WORK/main-health-stale"
tok="$(CHECKS_EC=0 MERGE_STATE=CLEAN HEAD_DATE=$H VERDICT="$STALE|true" BEHIND_BY=22 \
       MAIN_HEALTH_SENTINEL="$M_STALE" run 100)" || tok="exit-$?"
check "lazy main-health: stale-verdict lane token" "awaiting-review" "$tok"
probed "lazy main-health: stale-verdict lane does not probe" "no" "$M_STALE"

# ASSERTION (3) OF THE ANTI-MASKING PROOF, and the deadlock exception made
# mechanical: a lane that is already current never uses the relaxation, so it
# never asks about `main` — not one request, on the overwhelmingly common path.
M_CURRENT="$WORK/main-health-current"
tok="$(CHECKS_EC=0 MERGE_STATE=CLEAN HEAD_DATE=$H VERDICT="$FRESH|true" BEHIND_BY=0 \
       MAIN_HEALTH=red MAIN_HEALTH_SENTINEL="$M_CURRENT" run 100)" || tok="exit-$?"
check "lazy main-health: current lane token" "ready" "$tok"
probed "lazy main-health: current lane does not probe" "no" "$M_CURRENT"

# ASSERTION (2): the behind lane really does make the call. Without this, a
# probe deleted from pr-ready.sh would leave every green-default case passing.
M_BEHIND="$WORK/main-health-behind"
tok="$(CHECKS_EC=0 MERGE_STATE=CLEAN HEAD_DATE=$H VERDICT="$FRESH|true" BEHIND_BY=22 \
       THEIR_FILES="creek/classify/privacy.py" OUR_FILES="creek/vault/writer.py" \
       MAIN_HEALTH_SENTINEL="$M_BEHIND" run 100)" || tok="exit-$?"
check "lazy main-health: behind-past-merge-base lane token" "ready" "$tok"
probed "lazy main-health: behind lane DOES probe" "yes" "$M_BEHIND"

# PRECEDENCE: the merge-base validation runs FIRST, so a malformed merge base
# still short-circuits to `behind` without paying for the main-health call. A
# probe placed above that validation would spend a request to answer a question
# the lane had already answered.
M_BADBASE="$WORK/main-health-badbase"
tok="$(CHECKS_EC=0 MERGE_STATE=CLEAN HEAD_DATE=$H VERDICT="$FRESH|true" BEHIND_BY=22 \
       MERGE_BASE="not-a-sha" THEIR_FILES="creek/a.py" OUR_FILES="creek/b.py" \
       MAIN_HEALTH_SENTINEL="$M_BADBASE" run 100)" || tok="exit-$?"
check "malformed merge base still short-circuits to behind" "behind" "$tok"
probed "malformed merge base does not pay for main-health" "no" "$M_BADBASE"

# --- ready-unreviewed: PROVABLY no review gate ------------------------------
# `.github/workflows/code-review.yml` skips its `claude-review` job on Dependabot
# PRs (Actions secrets are not exposed to dependabot runs), so those PRs can never
# earn an LGTM — `awaiting-review` forever. `ready-unreviewed` is the narrow
# escape, and every one of its four conditions is load-bearing: bot-authored PR,
# bot-authored HEAD commit, at least one real non-review SUCCESS, and every
# `claude-review` rollup entry exactly SKIPPED. Miss any and this token becomes
# "merge without review" on a PR a human still owes a look at.
DEPENDABOT="app/dependabot"
DEPENDABOT_COMMIT="dependabot[bot]"
SKIPPED="SKIPPED"
NO_VERDICT="|false"

check "reviewless dependabot bump, green + fresh → ready-unreviewed" "ready-unreviewed" \
  "$(CHECKS_EC=0 MERGE_STATE=CLEAN HEAD_DATE=$H VERDICT="$NO_VERDICT" BEHIND_BY=0 \
     PR_AUTHOR="$DEPENDABOT" HEAD_AUTHOR="$DEPENDABOT_COMMIT" \
     REVIEW_CONCLUSIONS="$SKIPPED" run 100)"

# Re-runs leave several `claude-review` rollup entries; all SKIPPED is still no gate.
check "duplicate SKIPPED review entries → ready-unreviewed" "ready-unreviewed" \
  "$(CHECKS_EC=0 MERGE_STATE=CLEAN HEAD_DATE=$H VERDICT="$NO_VERDICT" BEHIND_BY=0 \
     PR_AUTHOR="$DEPENDABOT" HEAD_AUTHOR="$DEPENDABOT_COMMIT" \
     REVIEW_CONCLUSIONS="$SKIPPED,$SKIPPED" run 100)"

# A real verdict outranks the shortcut: the reviewed path must still report plain
# `ready`, so the loop's own logs distinguish "reviewed" from "no review exists".
check "reviewless setup WITH a fresh LGTM → ready" "ready" \
  "$(CHECKS_EC=0 MERGE_STATE=CLEAN HEAD_DATE=$H VERDICT="$FRESH|true" BEHIND_BY=0 \
     PR_AUTHOR="$DEPENDABOT" HEAD_AUTHOR="$DEPENDABOT_COMMIT" \
     REVIEW_CONCLUSIONS="$SKIPPED" run 100)"

# Skipping review does NOT skip freshness or mergeability. A bump changes the
# risk surface BY DEFINITION — its lockfile is the thing that decides how the
# whole tree lints and builds — so it re-proves itself every time `main` moves,
# no matter how unrelated main's commits look. This is PR #943's case: a `ruff`
# bump 22 behind, whose own green said nothing about the 22 commits after it.
check "reviewless bump 22 behind (its own lockfile) → behind" "behind" \
  "$(CHECKS_EC=0 MERGE_STATE=CLEAN HEAD_DATE=$H VERDICT="$NO_VERDICT" BEHIND_BY=22 \
     THEIR_FILES="creek/unrelated.py" OUR_FILES="creek-tools/uv.lock,creek-tools/pyproject.toml" \
     PR_AUTHOR="$DEPENDABOT" HEAD_AUTHOR="$DEPENDABOT_COMMIT" \
     REVIEW_CONCLUSIONS="$SKIPPED" run 100)"

# The twin, so the check above is provably about the risk surface and not about
# `ready-unreviewed` losing its freshness gate: the same reviewless lane, equally
# far behind, touching only ordinary source, still clears.
check "reviewless bump 22 behind, inert both sides → ready-unreviewed" "ready-unreviewed" \
  "$(CHECKS_EC=0 MERGE_STATE=CLEAN HEAD_DATE=$H VERDICT="$NO_VERDICT" BEHIND_BY=22 \
     THEIR_FILES="creek/unrelated.py" OUR_FILES="creek/other.py" \
     PR_AUTHOR="$DEPENDABOT" HEAD_AUTHOR="$DEPENDABOT_COMMIT" \
     REVIEW_CONCLUSIONS="$SKIPPED" run 100)"

check "reviewless but DIRTY merge state → behind" "behind" \
  "$(CHECKS_EC=0 MERGE_STATE=DIRTY HEAD_DATE=$H VERDICT="$NO_VERDICT" BEHIND_BY=0 \
     PR_AUTHOR="$DEPENDABOT" HEAD_AUTHOR="$DEPENDABOT_COMMIT" \
     REVIEW_CONCLUSIONS="$SKIPPED" run 100)"

# A human PR whose review job happened to be SKIPPED (a path filter, a cancelled
# run) is exactly the case where merging unreviewed would be worst.
check "human-authored PR with a SKIPPED review → awaiting-review" "awaiting-review" \
  "$(CHECKS_EC=0 MERGE_STATE=CLEAN HEAD_DATE=$H VERDICT="$NO_VERDICT" BEHIND_BY=0 \
     PR_AUTHOR="Geoffe-Ga" HEAD_AUTHOR="$DEPENDABOT_COMMIT" \
     REVIEW_CONCLUSIONS="$SKIPPED" run 100)"

# EVERY entry must be SKIPPED. One that actually RAN means a review gate exists,
# so its verdict — not this shortcut — decides.
check "one review entry that ran (SKIPPED,SUCCESS) → awaiting-review" "awaiting-review" \
  "$(CHECKS_EC=0 MERGE_STATE=CLEAN HEAD_DATE=$H VERDICT="$NO_VERDICT" BEHIND_BY=0 \
     PR_AUTHOR="$DEPENDABOT" HEAD_AUTHOR="$DEPENDABOT_COMMIT" \
     REVIEW_CONCLUSIONS="$SKIPPED,SUCCESS" run 100)"

# An EMPTY conclusion is a review still queued — the review that is about to
# happen. Treating empty as "not a blocker" merges out from under it.
check "queued review entry (empty conclusion) → awaiting-review" "awaiting-review" \
  "$(CHECKS_EC=0 MERGE_STATE=CLEAN HEAD_DATE=$H VERDICT="$NO_VERDICT" BEHIND_BY=0 \
     PR_AUTHOR="$DEPENDABOT" HEAD_AUTHOR="$DEPENDABOT_COMMIT" \
     REVIEW_CONCLUSIONS="$SKIPPED," run 100)"

# No `claude-review` entry at all proves nothing — the workflow may simply not
# have been dispatched yet. "Vacuously all SKIPPED" must not clear the gate.
check "no claude-review entry at all → awaiting-review" "awaiting-review" \
  "$(CHECKS_EC=0 MERGE_STATE=CLEAN HEAD_DATE=$H VERDICT="$NO_VERDICT" BEHIND_BY=0 \
     PR_AUTHOR="$DEPENDABOT" HEAD_AUTHOR="$DEPENDABOT_COMMIT" \
     REVIEW_CONCLUSIONS="" run 100)"

check "review-gate lookup failure → awaiting-review" "awaiting-review" \
  "$(CHECKS_EC=0 MERGE_STATE=CLEAN HEAD_DATE=$H VERDICT="$NO_VERDICT" BEHIND_BY=0 \
     PR_AUTHOR="$DEPENDABOT" HEAD_AUTHOR="$DEPENDABOT_COMMIT" \
     REVIEW_CONCLUSIONS="$SKIPPED" REVIEW_EC=1 run 100)"

# If the ONLY checks are skipped ones, `gh pr checks` can exit 0 with nothing
# having actually passed. Requiring a real non-review SUCCESS keeps "no checks
# ran" from reading as "CI is green".
check "zero non-review SUCCESSes → awaiting-review" "awaiting-review" \
  "$(CHECKS_EC=0 MERGE_STATE=CLEAN HEAD_DATE=$H VERDICT="$NO_VERDICT" BEHIND_BY=0 \
     PR_AUTHOR="$DEPENDABOT" HEAD_AUTHOR="$DEPENDABOT_COMMIT" \
     REVIEW_CONCLUSIONS="$SKIPPED" NON_REVIEW_SUCCESSES=0 run 100)"

check "one non-review SUCCESS → ready-unreviewed" "ready-unreviewed" \
  "$(CHECKS_EC=0 MERGE_STATE=CLEAN HEAD_DATE=$H VERDICT="$NO_VERDICT" BEHIND_BY=0 \
     PR_AUTHOR="$DEPENDABOT" HEAD_AUTHOR="$DEPENDABOT_COMMIT" \
     REVIEW_CONCLUSIONS="$SKIPPED" NON_REVIEW_SUCCESSES=1 run 100)"

# --- REAL jq: exercise the production rollup expression --------------------
# The scalar stub above can't catch a `--jq` that miscounts: `statusCheckRollup`
# mixes check runs and commit statuses, and a queued entry's `conclusion` is JSON
# `null`, not a string. These two feed a real payload through pr-ready.sh's own
# expression, so a `select(.conclusion == "SUCCESS")` that accidentally counts
# nulls or SKIPPEDs is caught here.
if command -v jq >/dev/null 2>&1; then
  REVIEW_ENTRY='{"name":"claude-review","conclusion":"SKIPPED"}'
  rj() { # rj <extra rollup entries> — a real --json author,statusCheckRollup payload
    printf '{"author":{"login":"app/dependabot"},"statusCheckRollup":[%s,%s]}' \
      "$REVIEW_ENTRY" "$1"
  }

  # Nothing actually passed: one skipped check and one still queued (null).
  check "real rollup, no non-review SUCCESS → awaiting-review" "awaiting-review" \
    "$(CHECKS_EC=0 MERGE_STATE=CLEAN HEAD_DATE=$H VERDICT="$NO_VERDICT" BEHIND_BY=0 \
       HEAD_AUTHOR="$DEPENDABOT_COMMIT" \
       ROLLUP_JSON="$(rj '{"name":"ci","conclusion":"SKIPPED"},{"name":"lint","conclusion":null}')" \
       run 100)"

  # One genuine SUCCESS alongside the same noise → the shortcut is earned.
  check "real rollup with a non-review SUCCESS → ready-unreviewed" "ready-unreviewed" \
    "$(CHECKS_EC=0 MERGE_STATE=CLEAN HEAD_DATE=$H VERDICT="$NO_VERDICT" BEHIND_BY=0 \
       HEAD_AUTHOR="$DEPENDABOT_COMMIT" \
       ROLLUP_JSON="$(rj '{"name":"ci","conclusion":"SUCCESS"},{"name":"lint","conclusion":null}')" \
       run 100)"
else
  echo "  skip - real-jq review-gate rollup cases (jq not installed)"
fi

# The PR author is not enough: anyone can push a commit onto a Dependabot branch
# (that is how we fix a bump's fallout), and that commit is unreviewed code
# riding a "no review needed" PR. The HEAD commit's author must be the bot too.
check "human commit on top of a bot PR → awaiting-review" "awaiting-review" \
  "$(CHECKS_EC=0 MERGE_STATE=CLEAN HEAD_DATE=$H VERDICT="$NO_VERDICT" BEHIND_BY=0 \
     PR_AUTHOR="$DEPENDABOT" HEAD_AUTHOR="Geoffe-Ga" \
     REVIEW_CONCLUSIONS="$SKIPPED" run 100)"

# The two spellings are NOT interchangeable: GitHub reports the PR author as the
# app slug `app/dependabot` and the commit author as `dependabot[bot]`. Comparing
# a commit author against the app slug never matches — and comparing it loosely
# would match any login containing "dependabot".
check "commit author spelled as the app slug → awaiting-review" "awaiting-review" \
  "$(CHECKS_EC=0 MERGE_STATE=CLEAN HEAD_DATE=$H VERDICT="$NO_VERDICT" BEHIND_BY=0 \
     PR_AUTHOR="$DEPENDABOT" HEAD_AUTHOR="$DEPENDABOT" \
     REVIEW_CONCLUSIONS="$SKIPPED" run 100)"

# --- field-count parsing: a surplus `|` must fail CLOSED -------------------
# Both multi-field answers are split with `IFS='|' read -r a b c rest`. A stray
# `|` (in a login, a branch name, an injected value) shifts every field one place
# and silently turns a garbage parse into a merge decision. A non-empty `rest`
# means "this answer is not the shape I expected" — refuse.
check "surplus field in the review-gate answer → awaiting-review" "awaiting-review" \
  "$(CHECKS_EC=0 MERGE_STATE=CLEAN HEAD_DATE=$H VERDICT="$NO_VERDICT" BEHIND_BY=0 \
     PR_AUTHOR="$DEPENDABOT" HEAD_AUTHOR="$DEPENDABOT_COMMIT" \
     REVIEW_CONCLUSIONS="$SKIPPED" NON_REVIEW_SUCCESSES='1|x' run 100)"

# Same for the mergeState answer. Which refusal token it picks is an
# implementation detail (the head_date/head_author it recovered may be garbage);
# what is NOT negotiable is that it never merges on it.
no_merge_token "surplus field in the mergeState answer is never mergeable" \
  "$(CHECKS_EC=0 MERGE_STATE=CLEAN HEAD_DATE=$H VERDICT="$NO_VERDICT" BEHIND_BY=0 \
     PR_AUTHOR="$DEPENDABOT" HEAD_AUTHOR="$DEPENDABOT_COMMIT|x" \
     REVIEW_CONCLUSIONS="$SKIPPED" run 100)"

# The human hold outranks the no-review-needed shortcut too — otherwise the one
# class of PR that merges without review is also the one that ignores the brake.
check "opt-out on a would-be ready-unreviewed PR → optout" "optout" \
  "$(CHECKS_EC=0 MERGE_STATE=CLEAN HEAD_DATE=$H VERDICT="$NO_VERDICT" BEHIND_BY=0 \
     PR_AUTHOR="$DEPENDABOT" HEAD_AUTHOR="$DEPENDABOT_COMMIT" \
     REVIEW_CONCLUSIONS="$SKIPPED" PR_LABELS="dependencies,$OPTOUT" run 100)"

# --- review-gate probe is LAZY ---------------------------------------------
# Same rate-limit argument as the compare probe: the rollup query is only worth
# making when the answer can still change the outcome. PR_AUTHOR is set to the
# value that WOULD clear the gate in every case, so a lane that probes anyway is
# caught by the sentinel rather than by a wrong token.
R_LGTM="$WORK/review-lgtm"
tok="$(CHECKS_EC=0 MERGE_STATE=CLEAN HEAD_DATE=$H VERDICT="$FRESH|true" BEHIND_BY=0 \
       PR_AUTHOR="$DEPENDABOT" REVIEW_SENTINEL="$R_LGTM" run 100)" || tok="exit-$?"
check "lazy review-gate: fresh LGTM token" "ready" "$tok"
probed "lazy review-gate: fresh LGTM does not probe" "no" "$R_LGTM"

R_PENDING="$WORK/review-pending"
tok="$(CHECKS_EC=8 PR_AUTHOR="$DEPENDABOT" REVIEW_SENTINEL="$R_PENDING" run 100)" || tok="exit-$?"
check "lazy review-gate: pending token" "pending" "$tok"
probed "lazy review-gate: pending does not probe" "no" "$R_PENDING"

R_CIFAIL="$WORK/review-cifail"
tok="$(CHECKS_EC=1 PR_AUTHOR="$DEPENDABOT" REVIEW_SENTINEL="$R_CIFAIL" run 100)" || tok="exit-$?"
check "lazy review-gate: ci-failed token" "ci-failed" "$tok"
probed "lazy review-gate: ci-failed does not probe" "no" "$R_CIFAIL"

R_OPTOUT="$WORK/review-optout"
tok="$(CHECKS_EC=0 MERGE_STATE=CLEAN HEAD_DATE=$H VERDICT="$NO_VERDICT" BEHIND_BY=0 \
       PR_AUTHOR="$DEPENDABOT" HEAD_AUTHOR="$DEPENDABOT_COMMIT" \
       REVIEW_CONCLUSIONS="$SKIPPED" PR_LABELS="$OPTOUT" \
       REVIEW_SENTINEL="$R_OPTOUT" run 100)" || tok="exit-$?"
check "lazy review-gate: opt-out token" "optout" "$tok"
probed "lazy review-gate: opt-out does not probe" "no" "$R_OPTOUT"

# A fresh non-LGTM verdict on a bot PR: the verdict IS the review gate speaking,
# so it outranks the ready-unreviewed shortcut — `changes-requested` without ever
# paying for the rollup probe.
R_CR="$WORK/review-changes-requested"
tok="$(CHECKS_EC=0 MERGE_STATE=CLEAN HEAD_DATE=$H VERDICT="$FRESH|false" BEHIND_BY=0 \
       PR_AUTHOR="$DEPENDABOT" HEAD_AUTHOR="$DEPENDABOT_COMMIT" \
       REVIEW_CONCLUSIONS="$SKIPPED" REVIEW_SENTINEL="$R_CR" run 100)" || tok="exit-$?"
check "lazy review-gate: fresh non-LGTM token" "changes-requested" "$tok"
probed "lazy review-gate: fresh non-LGTM does not probe" "no" "$R_CR"

R_NOVERDICT="$WORK/review-noverdict"
tok="$(CHECKS_EC=0 MERGE_STATE=CLEAN HEAD_DATE=$H VERDICT="$NO_VERDICT" BEHIND_BY=0 \
       PR_AUTHOR="$DEPENDABOT" HEAD_AUTHOR="$DEPENDABOT_COMMIT" \
       REVIEW_CONCLUSIONS="$SKIPPED" REVIEW_SENTINEL="$R_NOVERDICT" run 100)" || tok="exit-$?"
check "lazy review-gate: no-verdict lane token" "ready-unreviewed" "$tok"
probed "lazy review-gate: no-verdict lane DOES probe" "yes" "$R_NOVERDICT"

# --- cross-file coupling: the review job's check NAME ----------------------
# pr-ready.sh identifies the review check by the literal string `claude-review`,
# which is the workflow's JOB KEY. Two edits to code-review.yml would silently
# wedge every Dependabot lane at `awaiting-review` with no test failing anywhere
# else: renaming the job key, or adding a `name:` override (GitHub then reports
# the check under the display name, and the literal match stops matching).
REVIEW_WORKFLOW="$(cd "$(dirname "$0")/../.." && pwd)/.github/workflows/code-review.yml"

if grep -qx "  claude-review:" "$REVIEW_WORKFLOW"; then
  ok "code-review.yml still defines the 'claude-review' job key"
else
  bad "code-review.yml no longer has the '  claude-review:' job key pr-ready.sh matches"
fi

# Keys one level inside the job block: stop at the next job (2-space key), skip
# comments, and drop everything after the first colon.
job_keys="$(awk '$0 == "  claude-review:" {j = 1; next}
                 j && /^  [^[:space:]#]/ {exit}
                 j && /^    [^[:space:]#]/ {sub(/:.*/, "", $1); print $1}' \
            "$REVIEW_WORKFLOW" 2>/dev/null || true)"

# Herestring, not a pipe: `grep -q` exits on its first match, so a `printf | grep -q`
# pipeline can report non-zero on a MATCH under `pipefail` (printf dies with SIGPIPE).
if grep -qx "name" <<<"$job_keys"; then
  bad "claude-review declares a name: override — GitHub will report a different check name"
else
  ok "claude-review declares no name: override (check name stays 'claude-review')"
fi

# --- summary ---------------------------------------------------------------
echo
echo "pr-ready tests: $PASS passed, $FAIL failed"
[[ "$FAIL" -eq 0 ]]
