# THE verdict selector. One file, read by every path that clears a merge.
#
# It used to be two hand-parallel copies — one in `scripts/ralph/pr-ready.sh`,
# one in `.github/workflows/iteration-trigger.yml` — plus around forty lines of
# comments in the tree warning that the copies had to stay parallel but not
# identical. That comment burden was the smell, not the mitigation: #1266 exists
# because a `.body != null` guard was written into one copy and not the other,
# and #1199's author allowlist had to be added to each copy separately for
# exactly the same reason.
#
# ------------------------------------------------------------------ CONTRACT
# INPUT   THE RAW ANSWER to `scripts/ralph/lib/pr-comments.graphql`, or the
#         legacy `{"comments": [ … ]}` envelope `gh pr view --json comments`
#         used to produce — the intake below accepts either, so every existing
#         fixture stayed valid across the move to GraphQL. Each element carries
#         `databaseId`, `body`, `createdAt`, `author.login` and (when the
#         comment has ever been edited) `userContentEdits.nodes[].editor.login`.
#
#         THE INTAKE LIVES HERE BECAUSE A RESHAPE IS A PLACE TO LOSE A FIELD.
#         Both consumers used to project the GraphQL answer down to
#         `{comments: …}` themselves — `pr-ready.sh` in its `$ENV` prelude,
#         `iteration-trigger.yml` in a `--jq` of its own — which is two more
#         hand-parallel copies of exactly the kind #1266 came from. Worse, every
#         behavioural test of this filter feeds it a HAND-BUILT envelope and so
#         runs past both of them: a projection narrowed to
#         `{body, createdAt, author}` strips `userContentEdits`, reopens #1263 in
#         full, and leaves the whole suite green. One intake, inside the program
#         that reads the fields, is the only shape where the tests below cover
#         the bytes production actually parses.
#
# PARAMS  `$authors` (array of accepted logins), `$verdict_re`,
#         `$verdict_lgtm_re`, `$iter_summary_re`, `$marker_re`,
#         `$marker_any_re`, `$marker_malformed`. Bound with `--argjson`/`--arg`
#         by `jq -f`, or by an `$ENV`-reading prelude where the caller runs this
#         through `gh --jq` (which has no `--arg`). Either way the values arrive
#         as raw strings: NOTHING here is spliced into a jq string literal, so
#         the doubled-backslash hazard the old inline copies carried (`\\s` had
#         to survive jq's own string parser) does not exist in this file. A
#         caller that hands over a doubled-backslash regex gets a pattern
#         matching a literal backslash, which selects nothing — silently, on
#         every lane at once. `creek-tools/tests/test_verdict_select_filter.py`
#         runs the constants `pr-ready.sh` actually declares through `--arg` for
#         precisely that reason.
#
# OUTPUT  ONE `-r` line, five `|`-separated fields:
#
#           createdAt | lgtm | marker | refused | databaseId
#
#         An empty first field means nothing was selected — a WAIT, never a
#         clearance. The field COUNT is load-bearing: `pr-ready.sh` splits on it
#         and blanks the whole answer on a surplus field, so no field here may
#         ever be able to contain a `|` that the caller has not accounted for.
#         An RFC3339 stamp, a jq boolean, a PR number, a GitHub login and a
#         decimal id can none of them contain one.
#
# FIELD 5 (`databaseId`) EXISTS SO NO CONSUMER HAS TO LOOK THE COMMENT UP AGAIN.
# `iteration-trigger.yml` needs the selected comment twice over — the numeric id
# for its "pull comment N" wake message, and the body to DISPLAY a non-LGTM
# verdict as CHANGES REQUESTED rather than COMMENTS — and it used to re-find it
# by `createdAt`. GitHub's stamps are second-granular and two comments in the
# same second are ordinary on an active PR, so `last` over the UNFILTERED list
# could hand back a comment this filter REFUSED: the id would name the wrong
# comment and the displayed verdict would come from a body the gate rejected.
# That contradicts the doctrine at the foot of this header. A `databaseId` is
# unique, so addressing the comment by it cannot select anything else.
#
# FIELD 4 (`refused`) IS DIAGNOSTIC ONLY. It feeds no token, no latch and no
# freshness comparison, and it is computed from the UNFILTERED tail on purpose.
# Two shapes, told apart by the separator so a consumer branches on structure
# rather than on prose:
#
#   `<login>`                     the latest verdict-bearing, non-summary comment
#                                 came from an account `$authors` does not name
#                                 (#1199).
#   `<login> edited-by:<editor>`  it came from an accepted author, but its body
#                                 was rewritten by somebody else (#1263).
#
# Without it, filtering at SELECTION makes an unmarked verdict invisible: the
# lane behaves exactly as if nothing had been posted, which is the wrong report
# when what happened is that the PAT was rotated or a body was forged. Every
# lane in the fleet would read `awaiting-review` — an in-flight token, so the
# watcher sleeps — with nothing anywhere saying why.
#
# ------------------------------------------------- THE TWO POLARITIES AT ONCE
# Refusing too little clears a forged merge. Refusing too much is worse in a
# different way: it unmarks every lane in the fleet at once, silently, because a
# filter that matches nothing is indistinguishable from "no verdict has been
# posted yet". Both edges are pinned by test, and three rules follow from the
# second one:
#
#   * A SELF-EDIT IS STILL ADMITTED. Refusing on "was this edited at all"
#     (`includesCreatedEdit`) rejects the reviewer fixing their own typo — and an
#     attacker holding that account can post a fresh LGTM anyway, so the refusal
#     buys nothing there and costs a wedged lane with no self-heal.
#   * AN UNEDITED COMMENT IS ADMITTED UNCHANGED. `all` over an empty edit list is
#     vacuously true, so the #1263 conjunct is a no-op on the shape every comment
#     in this repo's history has. Flipping `all` to `any` reddens every verdict
#     case at once, which is the coupling that polarity needs.
#   * `isMinimized` IS DELIBERATELY NOT READ. A forger does not hide their own
#     comment, so refusing minimized comments closes nothing — while an operator
#     collapsing a noisy thread would wedge every lane that thread governs. The
#     decision is written here rather than in a commit message because the next
#     reader will otherwise "harden" it.
#
# ------------------------------------------------------- WHY EVERY TEST IS IN
# ------------------------------------------------------- ONE `select()`
# Filtering INSIDE the selection leaves a genuine earlier refusal selected, so
# it still governs. Picking the latest verdict-shaped comment and rejecting it
# afterwards would let anyone bury a real CHANGES_REQUESTED under a later forged
# LGTM: the buried verdict would never be seen. The same argument covers
# `createdAt` — an implementation that filtered the author for the LGTM flag but
# took the stamp from the unfiltered tail would let an attacker supply the
# FRESHNESS of somebody else's stale review. One `$v`, one comment, every field.

# Every verdict-shaped comment with a readable body, oldest first.
#
# `.body != null` IS THE FIRST CONJUNCT, and its position is the whole of #1266.
# jq's `and` short-circuits left to right, which is exactly why the author
# clause #1199 added — sitting to the RIGHT of the body test — did not rescue
# it. Measured at jq-1.7.1: feeding `null` to `test()` is
# `null (null) cannot be matched, as it is not a string` and exit 5, which under
# either consumer's `set -euo pipefail` aborts the step outright. One
# null-bodied comment (a deleted comment, an API hiccup) then costs the lane its
# wake on every subsequent CI completion, because that comment stays in the
# window forever.
#
# The iteration-trigger summary is excluded HERE rather than downstream (#1181):
# that workflow quotes the verdict line back into its own summary comment, whose
# `**VERDICT**: <X>` line satisfies `$verdict_re`, which cannot carry a
# provenance marker, and which posts LAST on every lane. Without the exclusion
# the two clearance paths bootstrap each other off an echo.
#
# THE INTAKE IS THE FIRST THING THAT HAPPENS, and it accepts both shapes so the
# GraphQL answer never has to be projected by a caller first (see the CONTRACT
# note above). `.data…nodes` is tried first because that is what production
# feeds; `.comments` is the fixture envelope. Chaining `.foo` on `null` is null
# in jq rather than an error, so the GraphQL path simply evaluates to null on a
# fixture and falls through. `[]` is TRUTHY in jq (only `false` and `null` are
# not), so a real-but-empty `nodes` array does NOT fall through to `.comments`.
( .data.repository.pullRequest.comments.nodes // .comments // [] ) as $all

| [ $all[]
  | select(
      .body != null
      and ((.body | test($iter_summary_re)) | not)
      and (.body | test($verdict_re))
    )
] as $vc

# Logins are unique case-insensitively, so folding BOTH sides admits no account
# that could not already match and hedges the fleet-wide unmark an unexpected
# casing from the API would otherwise cause.
| ($authors | map(ascii_downcase)) as $accepted

# THE SELECTED VERDICT. Two conditions, both inside the `select`.
#
# (1) AUTHORSHIP (#1199). Exact equality via `index` on the array, never
#     `test()`: as a regex, `github-actions[bot]` is `github-actions` followed
#     by the character class `[bot]`, which matches the registrable login
#     `github-actionsb`. `index` on an ARRAY is element equality (not the
#     substring search the same builtin performs on a string input) and returns
#     the POSITION — so a match on the first member answers `0`, and `0` is
#     TRUTHY in jq, where only `false` and `null` are not.
#
# (2) EDIT PROVENANCE (#1263). An account with write/triage access can open an
#     accepted author's genuine `## Verdict: CHANGES_REQUESTED` and retype it as
#     `## Verdict: LGTM`. The author is untouched, `createdAt` is untouched, and
#     #1181's `<!-- creek-review pr=N -->` marker rides along inside the same
#     body — so the allowlist, the currency stamp and the provenance marker all
#     pass and both clearance paths clear. `userContentEdits` is the only field
#     that disagrees.
#
#     `all`, over EVERY revision, not just the newest. Edit history is
#     append-only: an attacker rewrites the body and the author then edits again
#     for any reason, at which point a `last`-only check waves the tampered text
#     through. A missing `userContentEdits` key and an empty node list both give
#     the empty array, where `all` is vacuously true.
#
#     `(.editor.login? // "")` FAILS CLOSED on a deleted account: GraphQL
#     returns `editor: null` there, the default makes it the empty string, and
#     the empty string equals no allowlisted author — so the comment is refused.
#     Letting the null reach `ascii_downcase` instead would abort the step.
| ( $vc
    | map(select(
        ((.author.login // "") | ascii_downcase) as $a
        | ($accepted | index($a))
          and ( [ .userContentEdits.nodes[]? | ((.editor.login? // "") | ascii_downcase) ]
                | all(. == $a) )
      ))
    | last
  ) as $v

# --- the diagnostic tail (field 4) ------------------------------------------
# Computed from the UNFILTERED tail: the latest verdict-bearing, non-summary
# comment, described if and only if it was not admitted.
| ($vc | last) as $latest
| (($latest | .author.login) // "") as $latest_login
| ($latest_login | ascii_downcase) as $latest_a
| ( [ $latest.userContentEdits.nodes[]? | (.editor.login? // "") ]
    | map(select((. | ascii_downcase) != $latest_a))
  ) as $foreign_edits
| ( if ($foreign_edits | length) == 0 then ""
    else ($foreign_edits | first
          # An edit by an account that no longer exists is still an edit by
          # somebody other than the author, and saying so beats printing an
          # empty name into an operator-facing message.
          | if . == "" then "a-deleted-account" else . end)
    end
  ) as $foreign_editor
| ( if (($accepted | index($latest_a)) | not) then $latest_login
    elif $foreign_editor != "" then ($latest_login + " edited-by:" + $foreign_editor)
    else ""
    end
  ) as $refused

# --- the answer -------------------------------------------------------------
# Every field is read off `$v` — the ONE selected comment — so no field can be
# supplied by a comment the gate refused. `$mk` is the FIRST whole-line
# provenance marker in that body, falling back to a sentinel when the body
# mentions the marker but carries no parseable one: "malformed" and "absent" are
# different facts and the caller treats them differently.
| ($v.body // "") as $b
| ( ($b | [scan($marker_re)] | flatten | first)
    // (if ($b | test($marker_any_re)) then $marker_malformed else "" end)
  ) as $mk

# THE LAST VERDICT LINE IS THE FINAL WORD, and this is not a presentation
# nicety — it is the whole of the LGTM flag both clearance paths gate on.
#
# A WHOLE-BODY `test($verdict_lgtm_re)` — which is what this was — answers "does
# ANY line of this body state LGTM", and `$verdict_re` admits leading whitespace
# (`^\s*`), so an indented QUOTE of a verdict line counts. A review that quotes
# `    ## Verdict: LGTM` while itself concluding `## Verdict: CHANGES_REQUESTED`
# is not a hypothetical on a PR that touches these very files, and under the
# whole-body test it read LGTM: `iteration-trigger.yml` then posts "You are
# cleared to squash merge" and `pr-ready.sh` prints `ready`. Re-verdicting
# inside one comment has the same shape without any quoting at all.
#
# `scripts/ralph/stats.py`'s `normalize_verdict` IS THE REFERENCE CONTRACT — it
# collects every verdict LINE and returns the LAST — and this now matches it, so
# the parity the consumers' comments claim is true by construction rather than
# by assertion.
#
# NO OFFSET ARITHMETIC AND NO HARD-CODED LITERAL. `match(…; "g")` gives every
# verdict anchor and `splits` gives the text after the last one; concatenating
# the last anchor's own matched `.string` back onto that tail rebuilds exactly
# the slice of `$b` that starts at the final verdict line, with nothing about
# `$verdict_re`'s internals assumed. `.offset` would be the obvious alternative
# and is avoided on purpose: this program also runs under gojq (via `gh --jq`),
# whose codepoint/byte offset semantics this repo has not measured, whereas
# `match`, `.string` and `splits` are core builtins in both engines. Testing
# that slice with `$verdict_lgtm_re` — which is `$verdict_re` plus `+lgtm`, so
# the anchor is re-matched rather than assumed — keeps the two regexes coupled
# exactly as their definitions already are.
#
# The legacy `## Verdict\nLGTM` shape survives: `[:*\s]` consumes the newline,
# so the anchor spans it and the rebuilt slice still carries the token.
| ( [ $b | match($verdict_re; "g") ] ) as $vmatches
| ( if ($vmatches | length) == 0 then ""
    else (($vmatches | last | .string) + ([ $b | splits($verdict_re) ] | last))
    end
  ) as $last_verdict_line

| ( ($v.createdAt // "")
    + "|" + (($last_verdict_line | test($verdict_lgtm_re)) | tostring)
    + "|" + $mk
    + "|" + $refused
    + "|" + (($v.databaseId // "") | tostring) )
