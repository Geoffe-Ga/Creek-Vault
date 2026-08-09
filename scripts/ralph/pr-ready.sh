#!/usr/bin/env bash
# scripts/ralph/pr-ready.sh
#
# Authoritative "is this lane safe to merge?" check for the Ralph orchestrator
# (ralph-tick.md Step 1). Prints exactly one status token and exits 0 (it is a
# query — a non-zero exit means a usage/tooling error, never a PR verdict):
#
#   ready            LGTM (fresh) + CI green + verified current with main → merge now
#   ready-unreviewed CI green (with real checks that actually passed) + verified
#                    current with main, but this PR HAS no review gate: NO
#                    verdict-bearing comment from an ACCEPTED AUTHOR was ever
#                    posted on it (#1199), Dependabot authored it AND pushed its
#                    HEAD commit, and `claude-review`
#                    reported SKIPPED → the orchestrator decides (see ralph-tick.md)
#   behind           LGTM (fresh) + CI green, but `main` has landed something
#                    since the merge base that can invalidate this branch's
#                    green — a cross-cutting change (lockfile / tool pin /
#                    workflow / check script / root conftest) or an edit to a
#                    file this branch also touches → sync first. Merely being
#                    behind is NOT enough; see the freshness guard below.
#   main-not-green   LGTM (fresh) + CI green, and this lane IS behind `main` — so
#                    it is about to invoke the #1157 relaxation below, whose
#                    whole justification is the full CI run on `push: main`. That
#                    run is not green (it failed, or nothing has concluded yet, or
#                    the answer was unreadable), so the backstop is not there to
#                    catch anything and the relaxation is SUSPENDED for the
#                    duration — the lane waits. It deliberately does NOT print
#                    `behind`: `behind`'s remedy is a sync, and syncing pulls the
#                    breakage into the lane. Fails closed — anything other than a
#                    `green` answer from the `main-health.sh` sibling, including a
#                    missing or non-executable helper, holds the lane (#1159).
#   review-quota-exhausted
#                    LGTM (fresh) + CI green + CLEAN, and this lane genuinely
#                    needs a sync (it would print `behind`) — but the
#                    `claude-review` quota is PROVEN exhausted, so the re-review
#                    that sync makes necessary cannot happen. Syncing now would
#                    destroy the only verdict the lane will ever get, so it
#                    waits. Remedy: nothing to do — the rate-limit window resets
#                    on its own, and the lane then reads `behind` and syncs
#                    normally. Fails closed in the INVERTED direction — see the
#                    polarity block below (#1160).
#   pending          CI still running (or no checks registered yet) → wait for a later wake
#   ci-failed        CI has a failing/errored check → Step 2 (ci-debugging)
#   changes-requested CI green + a FRESH verdict (posted after HEAD) exists and is
#                    not LGTM (CHANGES_REQUESTED / COMMENTS) → Step 2
#                    (address-feedback). This is Gate 4 FAILED — an actionable
#                    state, distinct from waiting (issue #1097).
#   awaiting-review  no verdict posted yet, or only a STALE one (it predates the
#                    HEAD commit, LGTM or not) → wait for (re-)review. A
#                    verdict-shaped comment from an account the review pipeline
#                    cannot post as is not "posted" for this purpose: it is
#                    skipped at selection and leaves no trace at all (#1199).
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
# four-part meaning (fresh LGTM + green CI + CLEAN + verified current), so the
# decision about what to do with a PR no reviewer ever saw is made visibly in
# `ralph-tick.md` and never silently here — and in THIS repo ralph-tick.md's
# Step 1 routing does not merge on this token, it hands the lane to a human.
#
# Three conditions beyond "Dependabot authored it" make that safe, because the
# token's whole justification is "green CI against current main replaces the
# review":
#   * NO verdict-bearing comment FROM AN ACCEPTED AUTHOR may exist on the PR —
#     the `verdict_comment_seen` latch, checked first inside
#     `review_gate_absent` and argued at its own block further down (#1181).
#     Such a comment carries a verdict LINE (whatever `VERDICT_RE` accepts) and
#     was posted by an account only the review pipeline can authenticate as, so
#     it is proof that SOMETHING reviewed this PR and the shortcut's premise —
#     "there is no review gate to wait for" — is already false, whatever became
#     of that verdict afterwards: refused for provenance, stale, or unreadable.
#     "FROM AN ACCEPTED AUTHOR" is load-bearing and is #1199's, not #1181's: an
#     outsider's verdict-shaped comment is skipped at SELECTION and therefore
#     latches nothing, because a drive-by commenter must not be able to park
#     every Dependabot bump at `awaiting-review` forever. See the latch block.
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
# older code) and must not gate a merge. The `createdAt` it compares is the
# SELECTED comment's, so the selector's exclusion of iteration-trigger.yml's
# summary (ITER_SUMMARY_RE) belongs to THIS guard as much as to the provenance
# one: before that exclusion, a stale review followed by a fresh summary handed
# this comparison the SUMMARY's stamp and read as fresh. The AUTHOR filter
# (#1199) belongs to it for the identical reason and is the sharper case: with
# `createdAt` taken from an unfiltered `| last`, an outsider could SUPPLY THE
# FRESHNESS for somebody else's stale LGTM just by commenting after it. Every
# field comes off one `$v`, which is what makes that unreachable. That hole
# predates #1181, which masked it (the summary was refused for want of a marker before
# freshness was ever consulted) rather than closing it. It is closed and pinned
# now.
#
# ---------------------------------------------------------------------------
# PROVENANCE GUARD: A VERDICT MUST NAME THE PR IT REVIEWED (#1181)
# ---------------------------------------------------------------------------
# A verdict also only counts when the comment carrying it carries the marker
# `.github/workflows/code-review.yml` prepends to every review it posts —
# `<!-- creek-review pr=N -->` — and N is THIS PR. The marker is read from the
# SAME comment `VERDICT_RE` already selected (one `--jq`, one `$v` binding), and
# a marker that is absent, malformed, or names another PR makes the verdict NO
# VERDICT AT ALL: it gates neither `ready` nor `changes-requested`, so the lane
# falls through to `awaiting-review` — and never FORWARD into `ready-unreviewed`,
# which is what the verdict-EXISTED latch at the parse site enforces.
#
# WHICH COMMENT "THE VERDICT" IS, HOWEVER, IS ITS OWN QUESTION, AND GETTING IT
# WRONG DEFEATS EVERYTHING BELOW. The selector takes the LAST comment matching
# VERDICT_RE, and in this repo that was routinely NOT a review at all:
# `.github/workflows/iteration-trigger.yml`'s executive summary matches
# VERDICT_RE too and posts last on every lane, so between #1181 landing and this
# fix the guard was refusing a comment that had never been a verdict and holding
# the entire fleet on it. ITER_SUMMARY_RE excludes that comment inside the same
# `--jq`; the measurement, the decision to anchor it, and the residual it accepts
# are argued at that constant, and the correction to this file's own
# fleet-wide-risk argument is in the third-polarity block below.
#
# IT IS A PROVENANCE ATTESTATION, NOT A WRONG-DIFF DETECTOR. The workflow
# computes `pr=` from `github.event.pull_request.number` — the same value it
# posts the comment to — so in normal operation `pr=` CANNOT disagree with the
# PR the comment lives on, and this parser therefore cannot, on its own, catch a
# reviewer that read the wrong diff. Its whole force is negative: it refuses any
# verdict that did not come from a pipeline version which runs the
# WORKFLOW-SIDE `reviewed_pr_number` cross-check, where the agent self-reports
# the number it actually passed to `gh pr diff`, the workflow compares it with
# `$PR_NUMBER`, and any mismatch fails the check with no comment posted. THAT
# cross-check is the detector; this marker is the proof it was in force. It is
# spelled out because the tempting "simplification" is to delete the cross-check
# believing the marker covers it — after which every marker in the fleet would
# attest to nothing at all.
#
# THE INCIDENT (2026-08-07, PR #1117). The `prompt:` never stated the PR number,
# and `actions/checkout` leaves the runner in DETACHED HEAD on
# `refs/pull/<N>/merge`, so the agent's first command — `gh pr view --json
# number,…` — died with "could not determine current branch: failed to run git:
# not on any branch". It then GUESSED: `gh pr list --state all --limit 20 --json
# number,title,headRefName,mergeCommit,commits`, read `git log`'s `Merge a332aec
# into 46182a6f`, matched that BASE parent against PR #1179's `mergeCommit.oid`,
# and reviewed #1179's `cryptography`-floor bump (`gh pr diff 1179`, `gh pr view
# 1179`, `gh pr checks 1179`). The workflow posted the resulting LGTM onto #1117
# (`PR_NUMBER: 1117`), and `pr-ready.sh 1117` printed `ready` — a 38-file
# authenticated HTTP surface one orchestrator tick from merging on a review of a
# dependency bump. FOR THE NEXT READER: the issue's `HEAD^1..HEAD` diff-range
# hypothesis is REFUTED, by the owner himself. There is no diff range anywhere
# in that workflow; the defect was PR-number RESOLUTION, and a range-selection
# "fix" would have changed nothing.
#
# NO GRANDFATHER CLAUSE FOR UNMARKED LEGACY VERDICTS — and a time-based one
# would be incorrect rather than merely lenient. `pull_request` runs execute the
# workflow file from the PR's OWN merge ref, not from `main`, so a branch cut
# before this change keeps emitting unmarked verdicts AFTER it lands. There is
# no cutoff T that ever expires, which makes a grandfather clause here
# indistinguishable from leaving the feature switched off.
# What failing closed actually cost was measured, not assumed: at the time of
# writing there were 7 open PRs — 5 Dependabot lanes (which take the
# `ready-unreviewed` path and consult no verdict at all), #1117 (held under
# `do-not-auto-merge`), and #1070, #943, #863, none of which carried ANY verdict
# comment. Zero re-reviews. Structurally the bill was already owed too: this
# change touches `.github/workflows/`, which is on RISK_SURFACE_RE below, so
# every open lane already faced a sync → HEAD advance → stale-verdict
# invalidation → re-review. What changes is routing, not quota.
#
# THE ROUTING COST, NAMED HONESTLY — IT HAS TWO PARTS.
#
# (1) THE WEDGE. The verdict is consulted BEFORE `branch_is_current`, so a
# refused verdict prints `awaiting-review` and never reaches the `behind` branch
# that would have dispatched the sync — and `awaiting-review` is in watch-pr.sh's
# IN_FLIGHT_TOKENS, so the watcher sleeps on it. That is a wedge, and the un-wedge
# is one push by anybody. It is the accepted price of never merging an unattested
# verdict.
# "ONE PUSH BY ANYBODY" IS TRUE ONLY BECAUSE THE SELECTOR NOW EXCLUDES
# iteration-trigger.yml's SUMMARY. While it did not, a push produced a fresh
# review comment and then, 15 seconds later, a fresh unmarked summary that the
# selector preferred — so the wedge survived every push, on every lane, with no
# self-heal at all. See ITER_SUMMARY_RE: the sentence is a claim about which
# comment gets selected, not just about pushing.
#
# (2) THE LATCH'S OWN, NARROWER ROUTING CHANGE, on a lane the wedge above does
# not describe: a Dependabot bump whose only verdict is STALE BUT PERFECTLY
# ATTESTED. Before the `verdict_comment_seen` latch that lane fell past the
# stale-verdict guard into `review_gate_absent`, cleared it, and printed
# `ready-unreviewed`. It now prints `awaiting-review`. That is CORRECT — a stale
# verdict is not a missing review gate, it is an out-of-date one, and the
# shortcut's precondition is absence — and the un-wedge is the same single push,
# which (being ours, not the bot's) makes `claude-review` runnable again and
# lands the lane back on the normal `ready` path. It is written down because an
# unnamed behaviour change is how the next reader concludes the latch is a bug.
#
# WHAT THIS IS NOT: THE MARKER IS NOT AN AUTHORSHIP CONTROL AND NEVER BECAME
# ONE. It is a public, hard-coded literal, so it proves only what an HONEST
# verdict attests to: that the comment was produced by a PIPELINE VERSION which
# runs the workflow-side `reviewed_pr_number` cross-check described above — the
# control that actually catches #1117's class — so drift, replay and legacy
# accidents are refused where before they merged. That is a provenance claim
# about the emitter, not an identity claim about the author, and anybody who can
# comment can copy the literal.
#
# WHAT DOES CONTROL AUTHORSHIP IS A SEPARATE, LATER GUARD (#1199), and until it
# landed the sentence here read "a forged verdict still clears the gate" — which
# was measured true: a forged marked LGTM from an outsider printed `ready`, and a
# forged LGTM posted AFTER a genuine CHANGES_REQUESTED printed `ready` as well,
# so one comment could both manufacture an approval and bury a refusal. The
# verdict selector now admits ONLY comments whose author is in
# `VERDICT_AUTHORS_JQ` — the two identities code-review.yml's `GH_TOKEN:` can
# authenticate as — and it does the filtering AT SELECTION, so an outsider's
# comment is skipped rather than refused and a genuine earlier verdict still
# governs. The full argument, the alternatives, and the two residuals live at
# that constant; the two guards COMPOSE (an accepted author still needs a
# matching marker, and a matching marker still needs an accepted author).
#
# WHAT AUTHORSHIP STILL DOES NOT COVER, so that no reader over-reads it either:
# the repo owner can hand-post a verdict, because the emitter posts AS the owner
# when the PAT exists — and an account with write access can EDIT an accepted
# author's comment body. Both are named and argued at the constant. Neither is a
# reason to treat the two guards as weaker than they are: between them they
# refuse every verdict from an account that cannot post as this pipeline, which
# is the entire population of "anybody who can comment".
#
# TWO ALTERNATIVES CONSIDERED AND REJECTED (recorded here, not as issues, for
# the same reason the #1160 block below records its own):
#   * an AGENT-REPORTED diff fingerprint (`reviewed_paths_sha256`) instead of a
#     number, which would attest to the DIFF rather than to the PR. Coherent —
#     but an agent hashing a sorted path list is fragile (renames, whitespace,
#     locale, its own truncation), and each false positive turns a good review
#     red for no gain over the number the workflow already knows for certain.
#   * a distinct `verdict-unverifiable` token outside IN_FLIGHT_TOKENS, making
#     the wedge above an actionable state instead of a wait. It adds routing
#     surface to watch-pr.sh, ralph-tick.md Step 1 and test_watch_pr.sh at once,
#     and a token the orchestrator has no route for is worse than a wait.
#     Revisit it if the wedge is ever actually observed FOR THE SHAPE IT DESCRIBES
#     — a genuine review whose marker this parser cannot admit. It has NOT been.
#     What WAS observed live, fleet-wide, was the selector preferring
#     iteration-trigger.yml's summary over the review (see ITER_SUMMARY_RE), which
#     is a wrong-comment bug and not an unverifiable verdict; a new token would
#     have dressed it up as a routing state instead of fixing it. Keeping the two
#     apart is the point of this note.
#
# AND IT IS NOT A SIBLING SCRIPT. `main-health.sh` and `review-quota.sh` are
# separate helpers because each asks a question this file has no other way to
# answer. This is a pure function of a string already fetched, computed in the
# same `--jq`, at zero extra API cost. Extracting it would force a second
# `--json comments` call, or an argv/`MAX_ARG_STRLEN` dance to hand a whole
# comment thread to a child process, or a duplicated `VERDICT_RE` selector in
# two files — and that last one destroys the same-comment invariant that is the
# only thing making the check mean anything.
#
# TOKEN PRECEDENCE (deliberate, pinned by test_pr_ready.sh): `optout` is checked
# before everything else; then CI state (`pending` / `ci-failed`), exactly as it
# always was; the verdict is only consulted once CI is green. So
# `changes-requested` never outranks `pending`/`ci-failed` — a lane whose CI is
# still running classifies `pending` even if the verdict already landed, and the
# wake arrives when CI resolves (green → `changes-requested`, red → `ci-failed`;
# both orchestrator-actionable, so nothing is lost — only ordered). On the green
# path the fresh-non-LGTM check sits exactly where `awaiting-review` used to be
# emitted, and it wins over the `ready-unreviewed` shortcut: a posted verdict
# proves a review gate exists, so the verdict — not the shortcut — decides.
# THAT SENTENCE IS ENFORCED IN TWO PLACES, and it needs both to stay true: the
# `verdict_lgtm == "false"` branch covers a FRESH NON-LGTM verdict, and the
# verdict-EXISTED latch covers every verdict the provenance guard REFUSES —
# whose fields that guard has already blanked, so the first branch can no longer
# see them. Without the latch, "this verdict is inadmissible" would arrive at
# `review_gate_absent` as "this PR has no review gate", which is the shortcut's
# precondition and not what an unreadable marker means (#1181).
# FAIL-CLOSED: an unreadable verdict lookup still dies (exit 2, nothing on
# stdout — the tooling-error contract above), and a malformed verdict answer
# degrades to `awaiting-review` (wait), NEVER to `changes-requested` — the one
# token that dispatches a fix worker must not fire on garbage.
#
# Freshness guard: `mergeStateStatus` is NOT a freshness signal. GitHub computes
# BEHIND only when the base branch enforces strict/up-to-date status checks,
# which this repo does not — so a branch many commits behind `main` reports
# CLEAN, and its own green CI proves nothing about today's `main`. Measured
# here: PR #943 reports MERGEABLE/UNSTABLE while the compare API says
# `behind_by: 22`, and #863 says 44. #943 is a `ruff` bump, and the identical
# situation upstream (a bump 17 behind carrying a ruff major) produced 144 lint
# errors against the then-current tree — merging its stale green would have
# turned `main` red. Freshness therefore comes from the compare API, and CLEAN
# is KEPT alongside it because DIRTY / CONFLICTING / BLOCKED / DRAFT / UNKNOWN
# are invisible to it. The probe is LAZY by design — only a lane that would
# otherwise print `ready` pays for it, so the orchestrator never burns a request
# per lane per wake on already-decided lanes.
#
# WHY IT IS `behind_by > 0` PLUS A REASON, NOT `behind_by > 0` ALONE (#1137):
# requiring `behind_by == 0` outright is a self-imposed strict-up-to-date rule,
# and it is quadratic in lane count — merging ONE lane pushes every OTHER open
# lane behind, and each of them then pays a sync, a ~14-minute CI round, and
# (because the new HEAD commit invalidates its LGTM under the stale-verdict
# guard above) a full re-review. Because that window is comparable to the
# interval between merges, lanes routinely went stale again WHILE re-proving
# themselves. Measured across #1022 landing: CI runs per PR went 1.00 → 1.61
# (max 5) and p90 PR latency went 15 → 104 minutes, with two of PR #1117's five
# CI+review rounds spent on `Merge origin/main` commits that changed no code.
#
# So a branch that is behind is stale only for a REASON: `main` landed something
# on the cross-cutting RISK_SURFACE_RE below (a lockfile, a tool pin, a workflow,
# a check script, a root conftest — the class #943 actually belonged to), or it
# touched a file this branch also touches, which is a genuine semantic
# interaction. Otherwise the branch's green still describes the tree it would
# merge into, and it merges. What backstops the residual risk is the full CI run
# on `push: main` — every squash-merge re-proves the merged result, so a stale
# green that slips through is caught on `main` rather than assumed away. That
# backstop is now VERIFIED, not assumed: a lane about to spend it asks the
# `main-health.sh` sibling whether the `push: main` run actually concluded green,
# and holds (`main-not-green`) when it did not.
#
# WHY IT IS A PRECONDITION, NOT A GATE (#1159): the sentence above is a premise,
# and until #1159 nothing in the loop had ever read the run it names. While
# `main` is red that premise is simply false — the "it gets caught on `main`"
# half of the argument is not happening — so the relaxation has no justification
# left and is SUSPENDED: the gate falls back to pre-#1157 strictness for the
# duration. Never weakened, never widened, and never converted into a forced
# sync. Three consequences follow, and each one is load-bearing:
#
#   * It is asked ONLY by a lane that is behind. `behind_by == 0` means the
#     relaxation is not being invoked at all, so there is no premise to check —
#     and that lane merges even while `main` is red. That is not an oversight,
#     it is proof — for the class of breakage that is a function of the merged
#     tree — and it is airtight there: `behind_by == 0` means `main`'s HEAD is
#     already an ancestor of this branch's head; `.github/workflows/ci.yml`
#     carries no `paths:` filter, runs the IDENTICAL job matrix on `push` and
#     `pull_request`, and its `actions/checkout@v7` has no `ref:` override — so
#     this PR's CI ran on `refs/pull/N/merge`, a tree that already CONTAINS
#     whatever broke `main`, and `gh pr checks` is per-HEAD-commit. Its green
#     therefore says the commit-content breakage is either absent from the
#     merged result or fixed by this very branch.
#     The proof says nothing about breakage that is NOT a function of the
#     git tree: a newly published advisory against an already-pinned
#     dependency, an expired credential, a yanked upstream package. A
#     `behind_by == 0` lane whose CI went green BEFORE such an event still
#     carries the identical pinned dependency and still reads `ready` — its
#     green is stale with respect to that class, and re-running its CI right
#     now could be red. This is not hypothetical: `main-health.sh`'s own
#     header records this gate's first live run catching `main` red on
#     exactly that class (PYSEC-2026-3552 against `cryptography` 49.0.0,
#     which turned `pip-audit` red on a tree nobody had touched — the two
#     commits bounding the blame range were both innocent). That residual is
#     a knowingly ACCEPTED risk, not a hole in the proof above — see the next
#     bullet for why closing it costs more than it buys.
#   * That is exactly the shape of the PR that FIXES `main` — current with
#     `main`, green on the merge ref. So the remedy for a red `main` merges with
#     no bypass label, no override, and no extra API call. Making this lane
#     also consult `main-health.sh` was considered and rejected: the PR that
#     FIXES `main` is itself `behind_by == 0` and green, so gating it on
#     `main` being green would make the remedy unmergeable while `main` is
#     red — precisely the deadlock this precondition exists to avoid, and
#     escaping it would require the bypass label this design deliberately
#     does not have. So for the commit-content class the deadlock is closed
#     by construction; the time/environment-triggered class named above is an
#     accepted residual, not a gap in that construction.
#     (It is also flake-immune: if `main`'s red was a flake, current+green lanes
#     keep merging and the loop keeps moving; if it was real, those lanes go red
#     too under the identical matrix and are held by `ci-failed` anyway.)
#   * The polarity is fail-CLOSED, and the probe sits BEFORE
#     `main_changes_are_inert`, not after it. A lane behind a lockfile bump would
#     otherwise print `behind`, whose remedy (`fleet.sh sync`) is actively
#     harmful with `main` red: it pulls the breakage into the lane, burns a
#     ~14-minute CI round, and turns the lane's own CI red — which this script
#     then classifies `ci-failed`, dispatching a fix worker onto a failure the
#     lane never caused.
#
# ---------------------------------------------------------------------------
# THE SECOND PRECONDITION: NEVER SPEND A VERDICT THAT CANNOT BE REPLACED (#1160)
# ---------------------------------------------------------------------------
# `behind`'s remedy is `fleet.sh sync`, which pushes a merge commit, advances
# HEAD, and therefore invalidates the lane's LGTM under the stale-verdict guard
# above. Normally that costs one re-review and nothing else. When the
# `claude-review` quota is exhausted the re-review cannot happen at all, so the
# sync permanently destroys the only verdict the lane will ever get. Observed on
# PR #1158: LGTM at 05:11:43Z, sync at 05:23:58Z, re-review rejected in 24
# seconds against a `seven_day` window that would not reset for three days. The
# loop did that to itself, with its own remedy.
#
# THE RULE IS NOT "never destroy the LGTM on sync". It is "NEVER DESTROY IT AT A
# MOMENT WHEN IT CANNOT BE REPLACED" — a precondition on the remedy, exactly the
# shape the #1159 block above establishes. Not a new merge gate, not a
# relaxation of an existing one, and never a forced sync.
#
# It is spent in the terminal `else` (the would-be-`behind` branch) and ONLY when
# all four of these hold:
#   * the lane would otherwise print `behind` — that is what the `else` means;
#   * `ready_token` is `ready`, NOT `ready-unreviewed`. A Dependabot lane with no
#     review gate has no verdict for a sync to destroy, so holding it would buy
#     nothing and cost a merge. It keeps syncing exactly as today;
#   * `merge_state` is `CLEAN`. A CONFLICTING/DIRTY lane's remedy IS the sync:
#     the conflict never self-resolves and the lane's LGTM dies at Gate 1
#     regardless of quota, so holding it would be a PERMANENT wedge with no
#     un-wedge path — the quota resets, the lane is still conflicted, and nothing
#     ever ran the one command that could fix it;
#   * `review-quota.sh` positively answered `exhausted`.
# Sitting in that `else` also gives `main-not-green` precedence for free
# (`branch_is_current` sets `main_health_hold`, so the `elif` fires first) and
# guarantees an already-held lane never pays for the probe — no explicit
# precedence code is needed, and adding some would be a second place to keep in
# sync with this one.
#
# THE FAIL-CLOSED POLARITY IS INVERTED WITH RESPECT TO `main-health.sh`.
# DO NOT HARMONISE THEM.
#   main-health.sh:  anything that is not `green` HOLDS the lane.
#   review-quota.sh: only a positively-proven `exhausted` HOLDS the lane.
#     `available`, `unknown`, an empty answer, a non-zero exit, a garbage word, a
#     MISSING helper and a NON-EXECUTABLE helper all fall through to today's
#     `behind` → sync.
#   provenance (#1181): anything that is not a well-formed marker naming THIS PR
#     REFUSES the verdict — main-health.sh's polarity, not review-quota.sh's.
# THE THREE POLARITIES ARE DELIBERATELY NOT UNIFORM; the first two are argued
# here and the third in the block that follows. Read both before making any of
# them agree.
# The first two are fail-closed in the IDENTICAL sense — prefer the recoverable error —
# and therefore take OPPOSITE actions, because the recoverable error differs.
# There, merging a stale green onto a broken tree buries the culprit and waiting
# one wake costs nothing. Here, holding a lane on an unproven claim wedges a
# fleet slot for up to seven days with no self-heal, and the trigger for a false
# `exhausted` (a payload format change) would be correlated across every lane at
# once — while a false `available` costs one wasted sync, which is exactly what
# the loop does today. The issue's own acceptance criterion settles it: "Fails
# closed: if reviewability cannot be determined, behave as today (sync), since
# merging stale is the worse error." A future reader who "makes the two
# consistent" either re-introduces #1160 or wedges the fleet; test_pr_ready.sh's
# inverted sweep and test_review_quota.sh's `never_exhausted()` pin both
# directions.
#
# ---------------------------------------------------------------------------
# THE THIRD POLARITY, AND WHY IT SIDES WITH `main-health.sh` (#1181)
# ---------------------------------------------------------------------------
# The provenance guard holds on doubt: an absent, malformed or mismatched marker
# refuses the verdict. That is main-health.sh's polarity, and the reason is not
# "two out of three win".
#   * It is a different KIND of object. Both siblings above are preconditions on
#     a REMEDY — they decide whether the sync a lane is about to be told to run
#     is safe. This is a property of the MERGE GATE itself, and a merge gate's
#     doubt-polarity is settled by the oldest rule in this file: never weaken a
#     gate. An unattributable review is not a review.
#   * Against `main-health.sh` the shape is identical. The unrecoverable error
#     is merging an unreviewed 38-file authenticated surface on somebody else's
#     LGTM; the recoverable one is one wait plus one push.
#   * Against `review-quota.sh` the inversion rests on two legs. Leg (1) — a
#     false hold wedges a lane for DAYS with no self-heal — is normally false
#     here: the remedy is a self-service push and lands in one tick. Leg (2) —
#     the false-positive trigger is CORRELATED FLEET-WIDE — DOES apply, and it is
#     the strongest argument against this polarity: a broken emitter unmarks
#     every verdict at once, so every lane holds at once.
#     WHAT NEUTRALISES LEG (2) IS THE CROSS-FILE COUPLING TEST. test_pr_ready.sh
#     greps the emitter's own `printf` format out of
#     `.github/workflows/code-review.yml`, renders it, and round-trips it
#     through THIS parser — and `ralph-recap-tests.yml` runs that suite on
#     changes to that workflow. Emitter and parser therefore cannot drift
#     without CI going red ON THE VERY PR THAT CAUSES THE DRIFT, before any lane
#     can hold. Those two facts are load-bearing TOGETHER: delete the round-trip
#     case, or the `paths:` entry that makes it run, and this polarity loses its
#     justification. Do not remove either believing the other covers it.
#
#     AND THAT ARGUMENT WAS INCOMPLETE WHEN IT WAS FIRST WRITTEN — SAY IT PLAINLY,
#     BECAUSE IT IS THE NEAR-MISS THE NEXT READER NEEDS. "A broken emitter" said
#     THE emitter, and there are TWO emitters of a VERDICT_RE-matching comment in
#     this repo. The second is `.github/workflows/iteration-trigger.yml`'s
#     executive summary, whose `**VERDICT**: <X>` line satisfies VERDICT_RE and
#     VERDICT_LGTM_RE, which cannot carry a `creek-review` marker, and which posts
#     LAST on every lane — so the selector picked it, the guard refused it, and
#     the correlated fleet-wide hold this block claims to have neutralised was
#     happening on EVERY lane, from the moment #1181 landed. Leg (1) fell with it:
#     the self-service push does NOT un-wedge that shape, because a push produces
#     a fresh review comment and then, 15 seconds later, a fresh unmarked summary.
#     Before the fix, the string `iteration-trigger.yml` appeared in this file
#     ZERO times, and no fixture ANYWHERE in test_pr_ready.sh carried an
#     iteration-trigger summary — so nothing anywhere went red. (The suite did
#     have multi-comment fixtures; what it did not have was the second emitter's
#     comment. A test bench that models one producer cannot fail on a second one
#     it has never heard of.) It was found by reading four
#     merged PRs' live comment threads (#906, #905, #904, #902 — 4 of 4) at
#     Gate 2.5 round 3.
#     The same coupling discipline now covers the second emitter: ITER_SUMMARY_RE
#     below excludes it, its bytes are read out of that workflow's own `MARKER:`
#     and asserted against this constant, and the behavioural cases are built from
#     those bytes rather than from the suite's own. What that costs, and the
#     residual it accepts, is argued at the constant. THE GENERAL LESSON IS NOT
#     "add one more coupling test": it is that a polarity argument of the form
#     "only a broken EMITTER can do this, and the coupling test catches the
#     emitter" is only as good as the enumeration of emitters behind it. Before
#     adding a third consumer of VERDICT_RE, enumerate again.
#
#     THAT SELECTOR HAS SINCE GROWN AN AUTHOR DIMENSION (#1199), and the lesson
#     transfers to it unchanged except in WHICH enumeration has to hold. What
#     leg (2) newly rests on is the enumeration of ACCEPTED IDENTITIES, and
#     getting it wrong is the identical correlated failure by another road: an
#     allowlist missing a member unmarks every verdict in the fleet the day the
#     emitter authenticates as that member. So it is coupled the same way, by the
#     same suite — the allowlist's bytes against iteration-trigger.yml's own
#     selector, and its CARDINALITY against code-review.yml's `GH_TOKEN:`
#     expression, which is the only thing that makes any login legitimate at all.
#     A third `|| secrets.OTHER_PAT` therefore goes red on the PR that adds it,
#     while the author who knows which account that secret is, is still here.
#     Before adding a fourth consumer of VERDICT_RE — or a third way for the
#     pipeline to authenticate — enumerate BOTH sets again.
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

# Paths on `main` that can turn ANY branch's green red no matter what that branch
# touched, because they change how the whole tree is built, linted, typed or
# tested. This is the list that keeps #1022's actual finding alive: PR #943 was a
# `ruff` bump sitting 22 commits behind, and the identical situation upstream (a
# bump 17 behind carrying a ruff major) produced 144 lint errors against the
# then-current tree. A lockfile, a tool pin, a workflow, a check script or a root
# `conftest.py` landing on `main` therefore still forces a full re-prove.
#
# Everything NOT on this list is inert with respect to a branch that does not
# touch it: two bug fixes in different modules cannot invalidate each other's
# CI run, and before this list existed each of them forced the other to re-run
# ~14 minutes of CI and a full re-review (#1137).
readonly RISK_SURFACE_RE='(^|/)(pyproject\.toml|uv\.lock|poetry\.lock|conftest\.py|\.pre-commit-config\.yaml|(requirements|constraints)[^/]*\.txt)$|^\.github/workflows/|^creek-tools/scripts/'

# GitHub's compare API returns at most 300 entries in `.files`. AT the cap the
# list is truncated, so "these two changesets are disjoint" is an answer we
# cannot support — and the whole point of the disjointness test is to skip a
# sync. Fail closed at the cap: a big merge re-proves itself the old way.
readonly COMPARE_FILE_CAP=300

# The ONE answer from `main-health.sh` that clears the #1157 relaxation. Every
# other token it can print — `red`, `pending`, `unknown` — plus an empty answer
# and a helper that would not run at all, holds the lane. See the precondition
# block in the header for why the polarity is this way round (#1159).
readonly MAIN_HEALTHY_TOKEN="green"

# The sibling that answers it, resolved by THIS script's own dirname exactly as
# watch-pr.sh:65-66 resolves pr-ready.sh — so a copy of the tree, a worktree, or
# a checkout at any path finds its own helper and never a stray one on PATH.
# Declared and assigned on separate lines because `readonly x="$(…)"` masks the
# command substitution's exit status (SC2155).
#
# It is invoked DIRECTLY, never as `bash "$MAIN_HEALTH_HELPER"`: a helper whose
# exec bit was dropped in packaging (the exact failure #1092 shipped and
# test_exec_bits.sh now guards) must read as "we could not ask", not as an
# answer. `bash` would happily run it and hand back a verdict from a file the
# repo considers uninstalled.
MAIN_HEALTH_HELPER="$(cd "$(dirname "$0")" && pwd)/main-health.sh"
readonly MAIN_HEALTH_HELPER

# The ONE answer from `review-quota.sh` that holds a would-be-`behind` lane, and
# the token that reports it. Note the INVERTED polarity against
# `MAIN_HEALTHY_TOKEN` two blocks up: there, every answer except one holds the
# lane; here, every answer except one lets it sync. Both are fail-closed — see
# the "SECOND PRECONDITION" block in the header for why the safe action is the
# opposite one in each. Do not harmonise them (#1160).
readonly REVIEW_QUOTA_EXHAUSTED_TOKEN="exhausted"
readonly REVIEW_QUOTA_HELD_TOKEN="review-quota-exhausted"

# The second sibling, resolved by THIS script's own dirname for the same reason
# as `MAIN_HEALTH_HELPER` above — a worktree, a copied tree or a checkout at any
# path must find its own helper and never a stray one on PATH. Declared and
# assigned on separate lines because `readonly x="$(…)"` masks the command
# substitution's exit status (SC2155).
#
# Invoked DIRECTLY, never as `bash "$REVIEW_QUOTA_HELPER"`: a helper whose exec
# bit was dropped in packaging (#1092's failure, guarded by test_exec_bits.sh)
# must read as "we could not ask" — which HERE means "sync as usual", the
# inverted polarity's safe answer. `bash` would run a file the repo considers
# uninstalled and hand back a hold from it.
REVIEW_QUOTA_HELPER="$(cd "$(dirname "$0")" && pwd)/review-quota.sh"
readonly REVIEW_QUOTA_HELPER

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

# The provenance marker `.github/workflows/code-review.yml` PREPENDS to every
# review comment it posts, with N interpolated by the WORKFLOW from
# `github.event.pull_request.number` — never from anything the review agent
# says. Read from the SAME comment `VERDICT_RE` selects; see the provenance
# block in the header for why a second scan of the thread would prove nothing.
#
# Written with literal spaces, `[0-9]` and a POSIX class only: not one backslash
# appears here, so the doubled-escape hazard documented three lines up (this text
# is spliced into a jq string literal, where `\s` is invalid and must arrive as
# `\\s`) cannot arise by construction.
#
# `(?m)` IS LOAD-BEARING AND IS WRITTEN INLINE ON PURPOSE, exactly as it is in
# VERDICT_RE. Under the PRODUCTION engine (Go's `regexp`; see the note below) the
# `m` flag is documented as off unless asked for, so without it `^`/`$` would
# mean `\A`/`\Z` and this pattern would only match a marker that IS the entire
# comment. Spelling the flag out is also what makes the pattern independent of
# the OTHER engine's default, which is a question this file deliberately does not
# answer — see the two-engine note below for how their agreement is established
# instead. With `(?m)` in force, `^` is what stops review prose that merely
# quotes a marker — a review of this very change quotes one — from attesting, and
# the tail anchor is what refuses `pr=100 7` on PR 100, where a lax
# `pr=([0-9]+)` would capture `100` and merge.
#
# AND TWO DIFFERENT REGEX ENGINES HAVE TO AGREE ABOUT ALL OF THAT. This pattern
# is never run by the `jq` binary in production: it is spliced into a `--jq`
# expression that `gh` evaluates in its OWN process with gojq
# (`github.com/itchyny/gojq`), whose `test`/`scan` are Go's `regexp` — RE2.
# `scripts/ralph/test_pr_ready.sh` pipes its fixtures through the SYSTEM `jq`
# instead, which is Oniguruma. That the two agree about THIS pattern is asserted
# from OBSERVED BEHAVIOUR, not from any claim about what either engine's flags
# default to. That claim was in this comment once, stated confidently for both
# engines; a reviewer read Oniguruma's default the other way, and nobody had
# measured either reading. Behaviour settles it and a documentation reading does
# not, so behaviour is what is recorded:
#   * Under the system `jq` (Oniguruma), this exact pattern was MEASURED to do
#     the three things the guard needs: a marker quoted mid-line does NOT match,
#     a marker on its own line DOES, and a CRLF-terminated marker line DOES.
#     Those are not anecdotes — they are three named cases in
#     `scripts/ralph/test_pr_ready.sh` ("marker embedded mid-line", "marker
#     present and MATCHING", "CRLF comment body still attests"), so the
#     observation re-runs on every CI pass rather than aging into a claim.
#   * Under gojq (RE2), the same property has been carrying production for
#     months: VERDICT_RE sets the same inline `m` flag (as `(?im)`) and depends
#     on the same line-anchored `^` to find `## Verdict:` at the END of a
#     multi-line `## Summary …` body, and it has been selecting verdict comments
#     correctly the whole time.
# Both engines also accept `[[:space:]]` and `[0-9]+`. They agree on every byte
# written here because this pattern is DELIBERATELY RESTRICTED to the constructs
# common to both, not because the suite and production share an engine. They do
# not.
#
# SO THE COUPLING TEST VALIDATES THIS PARSER AGAINST AN ENGINE PRODUCTION DOES
# NOT USE, which is as far as an offline suite can go — and the failure it cannot
# see is one-directional: an Oniguruma-only construct (a lookahead, a
# backreference, `\K`) passes the local suite GREEN and then fails LIVE, where
# RE2 refuses to compile it, the whole `--jq` errors out, `gh` exits non-zero and
# this script dies mid-classification on every lane at once. Keeping to the
# common subset is what makes the local green transferable. VERDICT_RE rests on
# the identical property, and this is the one place it is written down.
#
# The trailing `[[:space:]]*` tolerates a carriage return (and stray trailing
# blanks) on the marker's own line. It loosens nothing that matters — the line
# must still be the marker and then whitespace to its end — and it is the one
# cheap hedge against the correlated failure this whole guard's polarity depends
# on not happening: a comment body that came back CRLF-delimited would otherwise
# unmark every verdict in the fleet at once.
readonly MARKER_RE='(?m)^<!-- creek-review pr=([0-9]+) -->[[:space:]]*$'

# The loose probe that tells "no marker at all" (every verdict posted before
# #1181 landed) apart from "a marker this emitter cannot have produced" (the
# emitter and this parser have drifted). It picks WHICH diagnostic is printed
# and nothing else — both shapes refuse the verdict — so its one false-positive
# shape, a review whose prose mentions the word, costs a slightly wrong message.
readonly MARKER_ANY_RE='creek-review'

# What the `--jq` puts in the marker field when MARKER_ANY_RE matched and
# MARKER_RE did not. Safe as a sentinel because `$pr` is `^[0-9]+$`-validated
# above, so no real PR number can ever collide with it.
readonly MARKER_MALFORMED='malformed'

# ---------------------------------------------------------------------------
# THE SECOND EMITTER OF A VERDICT_RE MATCH, AND WHY IT IS EXCLUDED (#1181)
# ---------------------------------------------------------------------------
# Everything above assumes the only thing on a PR that matches VERDICT_RE is a
# review comment. IN THIS REPO THAT IS FALSE, and the comment that falsifies it
# posts LAST on every lane. `.github/workflows/iteration-trigger.yml` posts a
# four-line executive summary whenever CI completes and a review verdict exists:
#
#     <!-- iteration-trigger -->
#     **CI**: 10/10 Green
#     **VERDICT**: LGTM
#     **Action**: You are cleared to squash merge, ...
#
# Line 3 SATISFIES VERDICT_RE, through the `(?:#{1,6}\s+|\*\*)?` alternative that
# pattern deliberately tolerates: the leading `**` matches it, `VERDICT` matches
# `verdict` case-insensitively, the second `*` matches `[:*\s]`, and `**: LGTM`
# then satisfies VERDICT_LGTM_RE as well. Measured through these very patterns
# with jq: VERDICT_RE true, VERDICT_LGTM_RE true, `creek-review` marker NONE.
#
# IT IS NOT A VERDICT, IT IS A REPORT OF ONE. That workflow SELECTS the review
# comment (`[.[] | select(.body | test("(^|\\n)## Verdict:"))] | last`) and
# copies the verdict line out of it. It cannot carry `<!-- creek-review pr=N -->`
# and must not: that marker attests the CODE-REVIEW pipeline produced the
# comment, and this one is posted by a workflow that never read a diff.
#
# WITHOUT THIS EXCLUSION IT WEDGES THE WHOLE FLEET, because the selector takes
# the LAST match and the summary is always it. Merged PR #906's comment tail:
#   06:53:17Z  `## Summary\nPR #906 fixes false-positive "broke...` <- the review
#   06:53:32Z  `<!-- iteration-trigger -->\n**CI**: 4/7 Green...`   <- 15 s later
#   07:03:05Z  `<!-- iteration-trigger -->\n**CI**: 10/10 Green...` <- LAST
# Same shape on #905, #904 and #902 — 4 of 4 merged PRs. The provenance guard
# then refuses that summary on EVERY lane at once; `awaiting-review` is in
# watch-pr.sh's IN_FLIGHT_TOKENS so the watcher sleeps on it; and the un-wedge
# the routing-cost block promises ("one push by anybody") does NOT clear it — a
# push yields a fresh review comment and, 15 seconds later, a fresh unmarked
# summary. That is precisely the correlated fleet-wide hold the third-polarity
# block says this guard cannot afford.
#
# THE EXCLUSION IS LINE-ANCHORED, AND THAT IS A DECISION. Both directions fail
# closed — skipping a comment can only ever LOSE a verdict, never invent one — so
# what is being chosen is how much verdict is lost, not whether it is safe:
#   * a BARE SUBSTRING `test("<!-- iteration-trigger -->")` also skips any review
#     whose PROSE quotes the marker. Not hypothetical: a review of THIS VERY
#     CHANGE quotes it, and the identical shape is already written down for the
#     OTHER marker — at MARKER_RE above ("a review of this very change quotes
#     one"), in iteration-trigger.yml's own extraction, and in two named cases of
#     test_pr_ready.sh. The cost is not one lost verdict either — it is a lane
#     wedged at `awaiting-review`, i.e. the failure this exclusion exists to
#     remove, re-entering through a narrower door.
#   * anchoring costs nothing against every REAL summary: that workflow builds
#     the body with `printf '%s\n%s\n%s\n%s\n'` and `$MARKER` FIRST, so the marker
#     sits on line 1 at column 0 — exactly where code-review.yml puts
#     `creek-review`. The two markers' parse rules are then symmetric, and this is
#     the very pattern iteration-trigger.yml itself uses to read the creek-review
#     marker (`grep -oE '^<!-- creek-review pr=[0-9]+ -->[[:space:]]*$'`).
# THE RESIDUAL IS ACCEPTED AND NAMED: a review that quotes the marker on a line
# of its OWN (inside a fenced block) is still skipped. That is one verdict lost
# on one PR, recoverable by one re-review — never a fleet-wide hold.
#
# `(?m)` IS SPELLED OUT rather than relied upon, and the pattern is restricted to
# the constructs gojq/RE2 (production) and Oniguruma (the suite) share. Not one
# backslash appears in it, so the jq-string-literal hazard documented at
# VERDICT_RE (`\s` is invalid there and must arrive as `\\s`) cannot arise by
# construction. Those are MARKER_RE's rules for MARKER_RE's reasons — read its
# engine note above before changing a byte here. The tighter `^` alone, anchoring
# to the START OF THE BODY where the emitter always puts it, is rejected for the
# reason recorded there: it would rest on a flag DEFAULT, and a confident claim
# about those defaults was written down once, read the other way by a reviewer,
# and never measured by anybody.
#
# AND THE BYTES ARE COUPLED TO THE EMITTER, exactly as the marker round trip
# couples this file to code-review.yml: test_pr_ready.sh reads `MARKER:` out of
# iteration-trigger.yml and asserts THIS constant carries it, so renaming the
# marker there without teaching this parser turns CI red on the renaming PR
# instead of silently re-opening the wedge.
readonly ITER_SUMMARY_RE='(?m)^<!-- iteration-trigger -->[[:space:]]*$'

# ---------------------------------------------------------------------------
# WHO IS ALLOWED TO SAY IT: THE VERDICT AUTHOR ALLOWLIST (#1199)
# ---------------------------------------------------------------------------
# Everything above asks WHAT a comment says. Until this constant existed nothing
# asked WHO said it, and the header conceded that in writing. The consequence was
# measured live against the code as it stood, not reasoned about: a forged marked
# LGTM from an outsider printed `ready`, and a forged LGTM posted AFTER a genuine
# CHANGES_REQUESTED printed `ready` too. One comment, from any account that can
# type in the box, could both manufacture an approval and bury a refusal on the
# script ralph-tick.md merges on.
#
# THE SET HAS EXACTLY TWO MEMBERS, AND NOT BY CHOICE. `.github/workflows/
# code-review.yml`'s Post-review step runs with
# `GH_TOKEN: ${{ secrets.GEOFFE_GA_PAT || secrets.GITHUB_TOKEN }}`, which has
# exactly two outcomes: the comment is authored by the PAT's account when the PAT
# secret exists and by the Actions bot when it does not. Which one appears is a
# property of secret availability, not of the review — so an allowlist naming
# only one of them unmarks every verdict in the fleet the day the PAT is rotated
# out, which is precisely the correlated failure the third-polarity block above
# says this class of guard cannot afford. test_pr_ready.sh pins the set against
# that `GH_TOKEN:` expression itself, including its CARDINALITY, so a third
# `|| secrets.OTHER_PAT` goes red on the PR that adds it rather than silently
# posting verdicts nothing will ever accept.
#
# AND THE BOT'S LOGIN IS SPELLED `github-actions` HERE — NO `[bot]`, NO `app/`.
# THIS IS THE ONE FACT IN THIS BLOCK THAT WAS MEASURED RATHER THAN REASONED, AND
# IT CAME BACK THE OPPOSITE OF WHAT EVERY OTHER FILE IN THIS REPO SAYS. The same
# bot is rendered THREE different ways across the three payloads this pipeline
# reads, and the difference is the marshalling, not the account. Measured live
# against PR #943 (a Dependabot lane — Dependabot comments on its own PRs, which
# is what makes it the cheap probe for a bot-authored COMMENT):
#
#   gh pr view 943 --json author   → .author.login          = app/dependabot
#   gh pr view 943 --json comments → .comments[].author.login = dependabot
#   gh api repos/…/issues/943/comments → .[].user.login      = dependabot[bot]
#
# THIS PARSER READS THE MIDDLE ONE, so the bot member must be the BARE SLUG.
# `github-actions[bot]` — the spelling `DEPENDABOT_COMMIT_AUTHOR` uses, the
# spelling both skills use, the spelling the REST-based sibling below needs, and
# the obvious one to write — is a string `--json comments` CANNOT PRODUCE. Had it
# shipped, the PAT-absent half of this allowlist would have been dead on arrival:
# fail-closed, so never a forged merge, but the day `GEOFFE_GA_PAT` lapsed every
# verdict in the fleet would be skipped by the very member added to hedge exactly
# that. `app/dependabot` two spellings up is the same trap from the other end and
# is why `DEPENDABOT_AUTHOR` and `DEPENDABOT_COMMIT_AUTHOR` are two constants
# rather than one: this file has already been bitten by assuming one login
# spelling serves two fields. `gh` renders a GraphQL `Bot` actor differently per
# field, and no amount of reading either API's documentation settles which — only
# the three commands above do, which is this file's standing rule (see MARKER_RE's
# engine note and `DEPENDABOT_AUTHOR`'s "read off a live bump").
#
# `github-actions` IS NOT SQUATTABLE, and that was checked too rather than
# assumed, because a bare slug lives in the user-login namespace in a way
# `dependabot[bot]` does not: `gh api users/github-actions` → 404, and GitHub
# reserves the slug of a first-party App. The residual — GitHub one day freeing
# it — would be loud, not silent: the diagnostic below names the observed login on
# every lane at once.
#
# DO NOT REFORMAT THE LINE BELOW. Its bytes are asserted verbatim, with
# `grep -qF`, against iteration-trigger.yml's own `CLAUDE=` selector — the SECOND
# merge-clearance path, and the one that WINS when the two disagree (its summary
# short-circuits await-claude-review's per-event classification, so an author
# filter here alone would have moved #1199 one file over rather than closed it).
# A jq array literal is the one form both selectors can hold character for
# character, which is what makes the coupling checkable instead of inferred from
# two expressions that "look equivalent". A space after the comma, a reordering,
# or a shell-side second copy of the same set all break that.
#
# WHAT IS ASSERTED IS THE SAME SET, NOT THE SAME BYTES, AND THE DIFFERENCE IS THE
# POINT. That file reads the REST endpoint, whose `.user.login` spells the bot
# `github-actions[bot]` — so the two literals are deliberately NOT identical, and
# a reader who "fixes" the mismatch by copying one over the other breaks whichever
# file they copied into, silently and fleet-wide. test_pr_ready.sh therefore pins
# each file against ITS OWN payload's spelling and pins the divergence itself, so
# the trap fails a test instead of a fleet.
#
# MEMBERSHIP IS EXACT STRING EQUALITY — the whole login and nothing but the
# login, compared as data — AND NEVER `test()`, which compares it as a PATTERN.
# `test` is the idiom already in reach (VERDICT_RE, MARKER_RE and ITER_SUMMARY_RE
# all use it), and it is wrong here twice over. `test` is UNANCHORED, so
# `test("github-actions")` matches any login merely CONTAINING the slug —
# `github-actionsb`, `my-github-actions`, `github-actions-ci` — every one of them
# registrable for the price of a signup. And a login is not pattern-free text:
# under the REST spelling this set carried until the measurement above,
# `github-actions[bot]` reads as `github-actions` followed by the character class
# `[bot]`, one character from {b,o,t}, so even an anchored `test` would have
# admitted `github-actionsb`. The suite pins that mutant by name — "A7 near-miss
# login 'github-actionsb'" — alongside `Geoffe-Ga2` (substring), `GEOFFE-GA-X`
# (case-insensitive), `github-actions ` (trimming / `startswith`), and the two
# OTHER payloads' spellings of this very bot, `github-actions[bot]` and
# `app/github-actions`, which must be refused HERE precisely because
# `--json comments` can never produce them: admitting a string this API cannot
# emit widens the set for nothing and hides the marshalling trap above.
#
# BOTH SIDES ARE `ascii_downcase`d, AND THAT IS A DECISION IN THE DIRECTION OF
# THE FLEET, NOT OF THE ATTACKER. It admits nothing: GitHub logins are unique
# case-INSENSITIVELY, so an account differing from an allowlisted one only by
# case cannot be registered, and every near miss above survives folding
# unchanged (`geoffe-ga-x` is still not `geoffe-ga`; a trailing space is still a
# trailing space). What it buys is the cheap hedge against the one failure this
# guard's polarity cannot afford: if the API ever handed back a login in a
# casing other than the one hard-coded here, exact byte equality would unmark
# every verdict on every lane at once. That is the identical trade MARKER_RE
# makes with its trailing `[[:space:]]*` for CRLF, and it is made the same way
# here — a correlated fleet-wide hold is worth more than a construct saved.
#
# THE INPUT IS `(.author.login // "")`, AND THE `//` IS NOT DEFENSIVE PADDING.
# GraphQL returns `author: null` for a deleted or ghost account, and a partial
# payload can leave `{}`; without the default, `test()`/`contains()` on that null
# would THROW, the whole `--jq` would error, `gh` would exit non-zero, and this
# script would die mid-classification on every lane carrying such a comment. With
# it, the login is the empty string, the empty string is in no allowlist, and the
# comment is skipped. Fails closed, silently, per lane.
#
# NO `authorAssociation` CONJUNCT. It looks like free tightening and is not:
# `github-actions[bot]`'s association is not reliably `OWNER`/`MEMBER`, and a
# repo transfer would flip the association for EVERYONE at once — a fleet-wide
# unmark, the failure mode named three paragraphs up. The login is already
# unforgeable (GitHub sets it; nothing PR-controlled does), so the conjunct adds
# correlated risk and no strength.
#
# NO ENVIRONMENT OVERRIDE. A knob that switches off a merge gate is the
# anti-bypass shape this repo refuses everywhere else, and it would be reachable
# from any process that can set a variable before invoking this script. The set
# is hard-coded and coupled by test; changing it is a reviewed diff.
#
# THREE ALTERNATIVES CONSIDERED AND REJECTED, recorded here rather than as issues
# for the same reason the #1160 and #1181 blocks record their own:
#   * CORRELATING THE VERDICT WITH A SUCCESSFUL `claude-review` CHECK RUN on the
#     same HEAD. Pure redundancy under selection-time filtering: the forged
#     comment is never selected, so there is nothing left to correlate. Nor does
#     it reach the residual below — "latest wins" would still let a hand-posted
#     LGTM outrank a genuine CHANGES_REQUESTED, since both would correlate with
#     the same run. It would put a second rollup correlation on the merge-critical
#     path and owe its own fail-closed story for every way that lookup can fail.
#   * MOVING THE EMITTER TO `gh pr review`. `--json comments` returns ISSUE
#     comments only, so this means rewriting both parsers and the skills that read
#     them — and the author of a PAT-submitted review is the identical datum,
#     `Geoffe-Ga`. All of the cost, none of the structural gain.
#   * AN HMAC / PER-RUN NONCE in the comment body. This is the only thing that
#     would separate "Geoffe-Ga the pipeline" from "Geoffe-Ga the human", and it
#     needs a verifier-side secret this script does not have and cannot be given
#     (it runs on a developer's machine). A STATIC per-run token is not a
#     substitute: it is replayable onto a forged body, exactly as the header
#     already says of the #1181 marker.
#
# THE RESIDUAL, NAMED PLAINLY — this is what keeps the header's honesty:
#   * THE REPO OWNER CAN STILL HAND-POST A VERDICT, because the PAT posts AS the
#     owner and nothing distinguishes the two. They can also merge directly and
#     rewrite this file, so they are not the threat model; the threat model is
#     everybody else, and everybody else is now out.
#   * AN ACCOUNT WITH WRITE/TRIAGE CAN EDIT AN ACCEPTED AUTHOR'S COMMENT BODY.
#     GitHub exposes that (`includesCreatedEdit`), and refusing every edited
#     comment was considered and rejected here: a typo fix on a real review would
#     wedge that lane with no self-heal, which is the wrong trade for a
#     capability that already implies write access. Filed as a follow-up (#1263)
#     rather than solved inline, because the fix is a policy question about
#     edited reviews and not a parser change — and the shape that would actually
#     close it (refuse only an edit made by an account OTHER than the author, via
#     the `userContentEdits` history) is a different API call, not a stricter
#     predicate on the payload this `--jq` already has.
readonly VERDICT_AUTHORS_JQ='["Geoffe-Ga","github-actions"]'

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
# another the latest top-level verdict as "<createdAt>|<isLGTM>|<marker pr= value>"
# — one call for both halves of the verdict question, see below. `gh` applies the
# `--jq` itself, with gojq rather than the `jq` binary (see MARKER_RE's engine
# note), so the whole answer arrives already reduced to one scalar per call and
# no comment thread is ever piped through this shell. The HEAD author rides along
# here rather than in its own call: it is
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

# FOUR fields now: "<createdAt>|<isLGTM>|<marker pr= value>|<refused author>".
# The marker is captured from `$v` — the SAME comment the VERDICT_RE selector
# above already picked — because a whole-thread question ("does this PR have a
# matching marker anywhere?") answers yes for every PR that was ever reviewed
# once, and would vouch for a verdict comment that carries no marker (#1181).
# `[scan(...)] | flatten | first` rather than `capture`: `capture` THROWS on no
# match, which would turn "this verdict is unattested" — the single most common
# case while legacy verdicts are still on the fleet — into a failed `gh` call
# and a lane the orchestrator refuses to classify at all.
#
# THE ITER_SUMMARY_RE EXCLUSION IS PART OF THE SELECTOR, INSIDE THIS ONE `--jq`,
# and it has to be. Every field below — `createdAt`, the LGTM test, the marker —
# is read off the single `$v` binding, and that SAME-COMMENT INVARIANT is the
# only thing that makes any of them mean anything: a second selector for the
# freshness stamp, or a marker lookup rebound to the comment that was skipped,
# resurrects #1181's own failure with one extra step. test_pr_ready.sh's W3, W3b
# and W5 cases are what kill those three mutants; the "AND IT IS NOT A SIBLING
# SCRIPT" block in the header is the same argument against splitting the answer
# across files.
#
# IT ALSO CLOSES A PRE-EXISTING FRESHNESS HOLE, as a side effect worth naming:
# before this, a STALE review followed by a FRESH summary handed the stale-verdict
# guard the SUMMARY's `createdAt`, so the guard compared the wrong comment's time
# against HEAD and a review of code that is no longer there passed as fresh. The
# provenance guard has been masking that since #1181 landed (it refused the
# summary before freshness was ever consulted) rather than closing it. It is
# closed now, and pinned.
#
# AND THE AUTHOR FILTER IS PART OF THE SAME `select(...)`, ALONGSIDE THAT
# EXCLUSION, FOR A REASON THAT IS THE WHOLE CORRECTNESS ARGUMENT (#1199).
# "Select the latest verdict, THEN refuse it if the author is wrong" and "select
# the latest verdict FROM AN ACCEPTED AUTHOR" agree on every honest lane and
# differ on exactly the one an attacker controls. Under select-then-blank, a
# forger who posts a fake LGTM AFTER a genuine CHANGES_REQUESTED BURIES the
# refusal: the fake is selected, the fields are blanked, and the lane reads
# `awaiting-review` — an IN_FLIGHT token, so watch-pr.sh sleeps on it and the fix
# worker the real verdict was owed is never dispatched. One comment, on any lane,
# at any time, silently downgrades an actionable token to a wait. Filtering at
# SELECTION leaves the genuine refusal selected, so it still governs and still
# dispatches address-feedback. Same reasoning one field over for `createdAt`: an
# implementation that filtered the author for the LGTM flag but took the stamp
# from the unfiltered `| last` would let the attacker SUPPLY THE FRESHNESS of
# somebody else's stale review. One `$v`, one comment, every field.
#
# THE FOURTH FIELD IS DIAGNOSTIC ONLY, and it is deliberately computed from the
# UNFILTERED tail: the login of the latest verdict-bearing, non-summary comment
# if and only if that author is NOT accepted, else empty. It feeds no token, no
# latch and no freshness comparison — see the stderr block below for why it has
# to exist at all. It is defined inside this same `--jq` so the allowlist lives
# in exactly one place: a shell-side second copy of the set is a duplicate that
# can drift from the one the coupling test greps.
#
# BOTH REGEX/JQ ENGINES HAVE BEEN MEASURED TO ACCEPT THESE CONSTRUCTS, because
# this file's convention is behaviour over documentation-reading (see MARKER_RE's
# engine note). The `select(((.author.login // "") | ascii_downcase) as $a | …)`
# shape was run against a live PR through `gh pr view --json comments --jq` —
# i.e. gojq/RE2, the PRODUCTION engine — and evaluated correctly, and the same
# shape evaluates correctly under the system `jq` (Oniguruma) the suite runs.
# HONESTLY: that measures the CONSTRUCTS, not a negative case. The suite's
# fixtures cover the refusals under Oniguruma only, so the limitation is the
# one-directional one MARKER_RE's note already concedes — an engine-specific
# construct passes locally and fails live on every lane at once. `map`,
# `ascii_downcase` and `index` are common to both by construction.
#
# `index` ON AN ARRAY IS ELEMENT EQUALITY, not the substring search the same
# builtin performs on a string input — the input here is always the allowlist
# array. It returns the POSITION, so a match on the first member answers `0`,
# and `0` is TRUTHY in jq (only `false` and `null` are not): the one language
# where this idiom would silently invert is not this one. It is also the idiom
# iteration-trigger.yml already uses for its own label membership
# (`[.labels[].name] | index("do-not-auto-merge")`), so the two clearance paths
# read the same way as well as accepting the same set.
verdict_line="$(gh pr view "${gh_args[@]}" \
  --json comments \
  --jq "[.comments[] | select(.body != null
                              and ((.body | test(\"$ITER_SUMMARY_RE\")) | not)
                              and (.body | test(\"$VERDICT_RE\")))] as \$vc
        | ($VERDICT_AUTHORS_JQ | map(ascii_downcase)) as \$authors
        | (\$vc | map(select(((.author.login // \"\") | ascii_downcase) as \$a
                             | \$authors | index(\$a))) | last) as \$v
        | ((\$vc | last | .author.login) // \"\") as \$latest_login
        | (if ((\$latest_login | ascii_downcase) as \$a | \$authors | index(\$a))
           then \"\" else \$latest_login end) as \$refused
        | (\$v.body // \"\") as \$b
        | ((\$b | [scan(\"$MARKER_RE\")] | flatten | first)
           // (if (\$b | test(\"$MARKER_ANY_RE\")) then \"$MARKER_MALFORMED\" else \"\" end)) as \$mk
        | ((\$v.createdAt // \"\") + \"|\" + ((\$b | test(\"$VERDICT_LGTM_RE\")) | tostring) + \"|\" + \$mk + \"|\" + \$refused)")"
# Split by FIELD COUNT, exactly like the mergeState answer above and the rollup
# answer below: an RFC3339 stamp, a jq boolean, a PR number and a login can none
# of them contain `|`, so a FIFTH field means the answer is not the shape we asked
# for and every branch below must fail closed on it. The two-expansion split this
# replaces (`%%|*` / `#*|`) read `true|100` into the LGTM flag the moment the
# answer grew its third field — the `|`-seeking class this file has already
# proven exploitable once.
#
# THE LOGIN IS THE ONE FIELD HERE THAT IS USER-CHOSEN TEXT, so it is the one
# worth saying WHY about rather than asserting alongside the others: GitHub
# logins are alphanumeric plus hyphen, and an App's bot login adds only the
# `[bot]` suffix — no `|` is representable in either. That is a fact about
# GitHub, and this parser deliberately does not rely on it: a surplus field
# blanks the whole answer, including the new field, so an attacker who somehow
# did smuggle a separator buys a wait rather than a shifted field.
IFS='|' read -r verdict_date verdict_lgtm verdict_pr verdict_refused_author verdict_rest <<<"$verdict_line"
[[ -z "$verdict_rest" ]] || { verdict_date=""; verdict_lgtm=""; verdict_pr=""; verdict_refused_author=""; }

# --- the skipped-author diagnostic: stderr ONLY (#1199) ---------------------
# WITHOUT THIS, FILTERING AT SELECTION MAKES AN UNMARKED VERDICT INVISIBLE. That
# is the price of skipping rather than refusing: the lane behaves exactly as if
# the comment had never been posted, and "exactly as if nothing happened" is the
# wrong report when what happened is that the PAT was rotated to an account this
# allowlist does not name. Every lane in the fleet would print `awaiting-review`
# — an IN_FLIGHT token, so the watcher sleeps — with nothing anywhere saying why,
# and the operator's reflex (re-run the review) posts one more comment from the
# same unrecognised account. That is the correlated fleet-wide failure the
# third-polarity block says this guard cannot afford, so the guard has to be
# LOUD about the only symptom it produces.
#
# ONE SHAPE IT CANNOT NAME, SO THE CLAIM ABOVE IS BOUNDED HERE RATHER THAN LEFT
# TO BE DISCOVERED: if the latest verdict-bearing comment's author is `null` — a
# deleted or ghost account — `$latest_login` is `""`, which is in no allowlist, so
# the comment is correctly SKIPPED but `$refused` stays empty and nothing is
# printed. That is a gap in loudness, not in the gate, and it is not reachable by
# an attacker at post time (an account cannot delete itself between posting and
# this read). Naming the empty case in the message instead would fire the
# diagnostic on every lane whose thread merely ends in a chat comment, which is
# the false-alarm A13 exists to forbid — so the silence is deliberate, and the
# bound is written down.
#
# IT NAMES THE OBSERVED LOGIN AND BOTH ACCEPTED IDENTITIES, because either half
# alone leaves the operator guessing: the observed login says who spoke, and the
# accepted pair is what turns "unrecognised" into "rotate the allowlist or the
# secret". It fires on the CLEARED lane too — a later skipped comment behind an
# accepted one — since a lane that prints `ready` has no held token to notice
# instead, and a silent skip there is precisely how a rotated PAT would go
# unobserved until the whole fleet stalled.
#
# STDERR ONLY, and that is not a style preference: stdout carries the one token
# this script contracts to print, and test_pr_ready.sh's `run()` drops stderr
# while ~14 cases assert a BARE token. A diagnostic on stdout fails all of them
# at once and, worse, hands the orchestrator an unparseable answer for a lane
# that is otherwise fine. One `printf` per LINE with each fact whole within its
# line, matching the provenance guard's block below: the operator reads this in a
# log and the suite greps it line by line.
#
# WHO ACTUALLY SEES IT, STATED EXACTLY, BECAUSE THE PARAGRAPH ABOVE OVERCLAIMS IF
# LEFT ALONE. `ralph-tick.md` Step 1 runs `STATUS=$(scripts/ralph/pr-ready.sh
# "$PR_NUM")`, which captures stdout and lets stderr through — the orchestrator
# sees this. `watch-pr.sh` does NOT: it calls `bash "$READY" "$pr" 2>/dev/null`,
# so on the polling path these lines are discarded, and the same is already true
# of the provenance guard's diagnostic below. That is a real gap in the "be LOUD"
# argument and it is named rather than papered over — but simply deleting the
# `2>/dev/null` is the wrong fix and is deliberately NOT done here: that watcher
# re-runs this script every 30s for up to 30 minutes, so an unconditional
# passthrough reprints four lines ~60 times per wedged lane, on every lane at
# once, which is how a message stops being read. Surfacing it once per token
# CHANGE is the shape that works, it belongs in watch-pr.sh rather than here, and
# it is filed as #1270. Until then: a rotated PAT is loud in the orchestrator's
# log and silent in the watcher's.
#
# It says SKIPPED rather than refused on purpose. A refused verdict (the
# provenance guard) blanks fields that were selected; a skipped one was never
# selected, leaves no trace, and is inert — including in the latch below.
#
# THE ACCEPTED SET IS RENDERED FROM THE CONSTANT, NOT RETYPED INTO THE MESSAGE.
# Spelling the two logins out here would put a second copy of the allowlist in
# this file, and the copy the operator READS is the one that would drift: a
# message naming yesterday's identities is worse than no message, because it
# sends them to change the wrong thing. `$VERDICT_AUTHORS_JQ` prints as the jq
# array it is, which is also the exact text to paste when the set really must
# change.
if [[ -n "$verdict_refused_author" ]]; then
  {
    printf 'pr-ready: the latest verdict-bearing comment on PR #%s was posted by `%s`, which is not an account this review pipeline can post as — it was SKIPPED, not refused (#1199).\n' \
      "$pr" "$verdict_refused_author"
    printf 'pr-ready:   The accepted verdict authors are %s: the PAT identity when the GEOFFE_GA_PAT secret exists, and the Actions bot when it does not — the two outcomes of code-review.yml Post-review GH_TOKEN.\n' \
      "$VERDICT_AUTHORS_JQ"
    printf 'pr-ready:   If that login IS the review pipeline, the PAT has been rotated to an account VERDICT_AUTHORS_JQ does not name, and every lane in the fleet will read `awaiting-review` until it does.\n'
    printf 'pr-ready:   If it is not, nothing is wrong with this lane: a verdict-shaped comment from an outsider is inert here, and any earlier verdict from an accepted author still decides.\n'
  } >&2
fi

# --- the verdict-EXISTED latch, recorded BEFORE the guard blanks (#1181) ----
# The provenance guard immediately below is about to erase `verdict_date` and
# `verdict_lgtm`, and those two fields are the only trace a verdict comment
# leaves in this process. What must survive that erasure is NOT the verdict —
# refusing it is the whole point — but the bare FACT that one was posted, because
# `review_gate_absent` further down asks a different question and would otherwise
# answer it from evidence that no longer exists.
#
# THE TWO QUESTIONS ARE NOT THE SAME, AND CONFLATING THEM MOVES A LANE TOWARDS
# MERGING. `ready-unreviewed`'s precondition is "this PR HAS NO REVIEW GATE to
# wait for" — nobody has reviewed it and nobody ever will. An inadmissible marker
# says the verdict is UNUSABLE; it does not say the gate is ABSENT. A SELECTED
# comment carrying a `## Verdict:` line means SOMETHING reviewed this PR and
# spoke: the `claude-review` job posted it, or the repo owner hand-posted it as
# the PAT identity (the residual the allowlist block names). Either way the
# shortcut's premise is already false, and an unreadable marker is a reason to
# distrust WHAT was said, never evidence that nothing was said.
#
# Without this latch, the one lane where the difference is visible went the wrong
# way: a Dependabot bump whose every `claude-review` rollup entry really is
# SKIPPED, carrying an UNMARKED verdict. The guard blanks the fields, so
# `verdict_lgtm` is `""` and not `"false"` and the `changes-requested` branch can
# no longer fire; `review_gate_absent` consults only the author and the rollup,
# knows nothing of the verdict it never saw, and clears — and the lane prints
# `ready-unreviewed`, which is merge-adjacent. Refusing a verdict would then have
# pushed the lane FORWARD, leaving a PR that HAS a posted verdict further along
# than one that has none. That is the regression this latch closes, and it is
# what keeps the header's precedence sentence ("a posted verdict proves a review
# gate exists, so the verdict — not the shortcut — decides") true on the one path
# where the provenance guard, not the verdict, is doing the deciding.
#
# THE DISCRIMINATOR IS `$verdict_date`, DELIBERATELY — the same field the guard
# below uses to mean "a verdict actually exists", so the two can never disagree
# about whether there was one. NOT the marker field, which is EMPTY for precisely
# the case this latch exists to catch (a legacy verdict with no marker at all)
# and would therefore latch nothing exactly when it is needed.
#
# AND THE DISCRIMINATOR IS NOT "the rollup looks dirty". With no verdict-bearing
# comment at all, `$v` is null and every field of this answer comes back empty —
# that lane latches nothing and MUST still reach the shortcut. It is the control
# against the lazy fix: "stop `ready-unreviewed` firing after a refused verdict"
# must not degenerate into "delete `ready-unreviewed`", which re-wedges every
# Dependabot bump at `awaiting-review` forever, waiting on a review the workflow
# provably never runs (see the WHY `ready-unreviewed` EXISTS block in the header).
#
# THE SUMMARY EXCLUSION NARROWS WHAT FEEDS THIS LATCH, AND THAT WAS CHECKED
# RATHER THAN ASSUMED. `$verdict_date` now comes from a selector that skips
# iteration-trigger.yml's executive summary, so a summary is no longer evidence
# that a review gate exists. Exactly one lane could care: Dependabot author, every
# `claude-review` rollup entry SKIPPED, a summary on the thread, and NO review
# comment at all — `awaiting-review` before the exclusion, `ready-unreviewed`
# (merge-adjacent) after it.
#
# THAT LANE CANNOT OCCUR, and the proof is in the emitter, not in this file:
# iteration-trigger.yml EXITS EARLY ("No Claude review yet - skipping") unless its
# own selector finds a `## Verdict:` comment FROM AN ACCEPTED AUTHOR. So a summary
# exists only where an ACCEPTED-AUTHOR verdict comment already does — and every
# such comment matches VERDICT_RE (`##` takes the `#{1,6}\s+` alternative, then
# `verdict`, then `:`), carries no `<!-- iteration-trigger -->` line of its own,
# and is admitted by the author filter here as well, so it still latches. The
# invariant did not merely survive #1199, it TIGHTENED: the two selectors now
# share the allowlist byte for byte (a coupling test asserts it), so the set of
# comments that can produce a summary is a SUBSET of the set that latches here,
# which is the direction that keeps this argument sound. The latch is left feeding
# off the SAME corrected selector as everything else; it is deliberately NOT given
# a looser selector of its own, which would break the same-comment invariant for
# the sake of a lane that does not exist.
#
# IF THAT EARLY EXIT EVER LEAVES iteration-trigger.yml, this reasoning leaves with
# it, and the safe move is to fail CLOSED — latch on the summary as well — never
# to leave the latch reading a selector that can no longer see one. RE-CHECKED
# under #1199 and still true: the early exit is still there, and the author filter
# added to that selector only makes it fire MORE often, never less.
#
# AND THE LATCH IS FED BY THE AUTHOR-FILTERED `$verdict_date`, WITH NO CODE
# CHANGE AT ALL (#1199) — which is a decision, not an omission. Nothing about
# #1181 re-opens: every comment the selector can still pick is pipeline-authored,
# so a PIPELINE verdict that the provenance guard blanks set `$verdict_date`
# first and latches exactly as before. The one lane whose routing changes is a
# Dependabot bump whose only comment is an OUTSIDER's verdict: previously it
# latched and printed `awaiting-review`, now it prints `ready-unreviewed`, as if
# that comment had never been posted. That is CORRECT, and it is not a loosening
# — `review_gate_absent` still independently demands `app/dependabot` authorship,
# a Dependabot HEAD commit, at least one non-review SUCCESS, and an all-`SKIPPED`
# `claude-review` rollup, and not one of those is reachable by commenting.
#
# THE GOVERNING PRINCIPLE IS THAT AN UNAUTHORISED COMMENT MUST BE INERT, NOT
# MERELY NON-CLEARING. An author-BLIND latch ("a verdict-shaped comment exists,
# so a gate exists") reads as the conservative choice and is not: those bump lanes
# never get a `claude-review` run — code-review.yml skips the job because Actions
# secrets are withheld from Dependabot-triggered runs — so no verdict can ever be
# posted to clear the latch, and no push, sync or re-review self-heals it. One
# drive-by comment would park the bump forever, and repeating it across the fleet
# would stop dependency maintenance outright. Handing an outsider a DIFFERENT
# effect on routing is the same bug class as #1199 itself, one notch quieter.
#
# AND IT SITS AFTER THE FIELD-COUNT BLANK, one line up, not before it: a
# surplus-field answer is not evidence that a verdict exists, it is evidence that
# the answer is unreadable, and reading a latch out of fields we just refused to
# trust would be the same mistake in the other direction. The residual — a
# malformed answer letting a Dependabot lane keep the shortcut — is unreachable
# by construction, because an RFC3339 stamp, a jq boolean, a `[0-9]+` marker (or
# the `malformed` sentinel) and a GitHub login can none of them contain a `|`.
verdict_comment_seen=""
[[ -z "$verdict_date" ]] || verdict_comment_seen="yes"

# --- provenance: does this verdict attest to THIS PR? (#1181) ---------------
# STRING equality on purpose, and NOT for the reason it is tempting to give. A
# numeric `[[ "$verdict_pr" -eq "$pr" ]]` would not admit an absent marker:
# `[[ "" -eq 100 ]]`, `[[ "abc" -eq 100 ]]` and `[[ "malformed" -eq 100 ]]` are
# all FALSE on both shells this repo runs on, so the empty and lettered fields
# are precisely the ones `-eq` gets right.
#
# The hazard is a single value, and it is worse than a wrong answer — it is a
# DISAGREEMENT BETWEEN INTERPRETERS. `pr=0100` is the one point in the whole
# space where `-eq` and a string `!=` differ at all, and the two shells this file
# actually runs under differ from EACH OTHER about it. Measured, not assumed:
#
#   bash 5.3.15 (aarch64-apple-darwin24.6)  [[ "0100" -eq 100 ]] → TRUE,  -eq 64 → FALSE
#   /bin/bash 3.2 (stock macOS)             [[ "0100" -eq 100 ]] → FALSE, -eq 64 → TRUE
#
# (bash 5 reads the leading zero as decimal inside `[[ ]]`'s arithmetic context;
# bash 3.2 reads it as octal.) A numeric compare would therefore make the MERGE
# GATE SHELL-VERSION-DEPENDENT: the same marker clears on one interpreter and is
# refused on the other, with nothing in the output to say which one ran. String
# equality is exact and version-independent, and it is what the emitter produces:
# the workflow interpolates `github.event.pull_request.number` verbatim through a
# `%s`, so anything but the verbatim bytes is a marker it did not emit.
# test_pr_ready.sh pins this with a `pr=0100` case carrying the same measurements.
#
# Guarded on `$verdict_date` so the diagnostic describes a verdict that actually
# exists: with no verdict-bearing comment at all the marker field is empty too,
# and that is the ordinary `awaiting-review` lane, not a provenance failure.
if [[ -n "$verdict_date" && "$verdict_pr" != "$pr" ]]; then
  case "$verdict_pr" in
    "")
      marker_what='carries NO provenance marker'
      marker_tail='Expected for any verdict posted before #1181 landed: that pipeline never named the PR it reviewed.'
      ;;
    "$MARKER_MALFORMED")
      marker_what='carries a provenance marker this workflow cannot emit'
      marker_tail="The emitter and this parser have drifted; test_pr_ready.sh's coupling case should have caught it — fix the emitter, do not loosen the parser."
      ;;
    *)
      marker_what="names PR #$verdict_pr, not #$pr"
      marker_tail='A verdict produced for another PR, a forged comment, or an emitter bug. Do NOT merge. Unreachable in normal operation; file an issue against .github/workflows/code-review.yml.'
      ;;
  esac
  # One printf per LINE, and each fact whole within its own line: the operator
  # reads this in a log, and test_pr_ready.sh greps it line by line.
  {
    printf 'pr-ready: the latest verdict on PR #%s %s.\n' "$pr" "$marker_what"
    printf 'pr-ready:   %s\n' "$marker_tail"
    printf 'pr-ready:   It is treated as no verdict at all — it gates neither `ready` nor `changes-requested`, so this lane falls through to `awaiting-review` (#1181).\n'
    printf 'pr-ready:   Remedy: scripts/ralph/fleet.sh sync (or push any commit). That advances HEAD — which invalidated the old verdict anyway — and brings the marker-emitting workflow onto the branch, so the re-review is attested.\n'
    printf 'pr-ready:   Re-running the old review workflow run will NOT help: a re-run replays the workflow file from the commit that run was launched from, which is the one without the fix.\n'
  } >&2
  verdict_date=""
  verdict_lgtm=""
fi

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

# True only when this PR has no review gate to wait for: no verdict-bearing
# comment was ever posted (the latch above), Dependabot authored it, Dependabot
# also pushed its HEAD commit (so nothing of ours rides the branch — see the
# force-push note in the header), at least one non-review check actually
# SUCCEEDED (so "green" is not "nothing ran"), and every `claude-review` entry in
# its rollup reported SKIPPED (the rollup carries one entry per triggering event,
# so a single non-SKIPPED entry means the job did run and a verdict is genuinely
# owed). EVERY failure path fails CLOSED to `awaiting-review`: a failed call, an
# empty author, a malformed answer, or no `claude-review` entry at all all read
# as "the gate exists", so an unreadable answer can only ever hold the lane.
review_gate_absent() {
  local line author conclusions passes rest
  # THE LATCH, CHECKED FIRST (#1181). A verdict comment exists on this PR, so the
  # review gate EXISTS and this function's question is already answered NO — no
  # amount of author or rollup evidence can unsay a comment that is sitting on
  # the thread. The caller reaches here only because that verdict was REFUSED by
  # the provenance guard, is STALE, or came back MALFORMED — unusable, out of
  # date, unreadable. Not one of those is "there is no gate", and all three are
  # waits.
  #
  # THE ROLLUP IS NOT A SUBSTITUTE FOR THIS. `statusCheckRollup` is per-HEAD-
  # commit, so all-SKIPPED means the job did not run FOR THIS HEAD — the verdict
  # may have been posted against an earlier one, by a re-run, or by a pipeline
  # version this parser refuses. Every one of those makes the verdict unusable
  # and none of them makes the gate absent.
  #
  # Checked ahead of the `gh` call rather than after it because the answer cannot
  # change, and it spares the lane one API request per wake — the same laziness
  # argument every other probe in this file makes. (The `probed` sentinels in
  # test_pr_ready.sh pin the one path that MUST still pay for it: the lane with
  # no verdict comment at all.)
  [[ -z "$verdict_comment_seen" ]] || return 1
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

# The filenames a compare endpoint reports, one per line, or non-zero on any
# failure — including the 300-entry cap, where GitHub has truncated the list and
# a disjointness answer would be unsupportable. `grep -c` is guarded because it
# exits 1 on an empty input, which is a legitimate answer (a range whose commits
# changed no files) and not an error.
compare_files() { # compare_files <slug> <range>
  local out count
  out="$(gh api "repos/$1/compare/$2?per_page=1" \
    --jq '(.files // [])[].filename' 2>/dev/null)" || return 1
  count="$(grep -c . <<<"$out" || true)"
  [[ "$count" =~ ^[0-9]+$ ]] || return 1
  [[ "$count" -lt "$COMPARE_FILE_CAP" ]] || return 1
  printf '%s\n' "$out"
}

# True when everything `main` has landed since the merge base is inert with
# respect to THIS branch: it touched no cross-cutting risk surface, and it
# touched no file this branch also touches. Either of those is a real reason to
# re-prove green; neither being present means the branch's existing green still
# describes the tree it would merge into.
#
# Fails CLOSED on every failure path, exactly like the caller: an errored or
# truncated compare reads as "not inert", whose remedy (a sync) is always safe.
main_changes_are_inert() { # main_changes_are_inert <slug> <base> <head_oid> <merge_base>
  local theirs ours f
  # THEIRS first: it decides the risk-surface case on its own, so a lane that is
  # behind a lockfile bump never pays for the second call.
  theirs="$(compare_files "$1" "$4...$2")" || return 1
  while IFS= read -r f; do
    [[ -n "$f" ]] || continue
    if [[ "$f" =~ $RISK_SURFACE_RE ]]; then return 1; fi
  done <<<"$theirs"
  ours="$(compare_files "$1" "$2...$3")" || return 1
  while IFS= read -r f; do
    [[ -n "$f" ]] || continue
    # OUR side of the risk surface counts too, and for the sharper reason. A
    # branch that changes how the tree is linted/built/typed/tested has proved
    # that only against the tree AT ITS MERGE BASE; everything `main` has landed
    # since is code the new tooling has never been run over. That is PR #943
    # exactly — the ruff bump whose own green said nothing about the 22 commits
    # that arrived after it — so a bump re-proves itself whenever `main` moves,
    # which is the case #1022 was right about and this keeps.
    if [[ "$f" =~ $RISK_SURFACE_RE ]]; then return 1; fi
    # -x -F: whole line, literal. A path is not a pattern, and a prefix match
    # would read `creek/vault.py` as overlapping `creek/vault_index.py`.
    if grep -qxF "$f" <<<"$theirs"; then return 1; fi
  done <<<"$ours"
  return 0
}

# Set by `branch_is_current` when the lane is held because `main` itself is not
# green — the ONE reason a not-current lane must NOT be told to sync. Module
# scope because the function reports through the exit status the `&&` below
# consumes, and "hold" and "sync first" are two different remedies (#1159).
main_health_hold=""

# True when `main` has landed nothing since the merge base that could invalidate
# this branch's green. `behind_by == 0` is the fast path and still short-circuits
# on ONE call, so a lane that is already current costs exactly what it always
# did; only a lane that is genuinely behind pays for the file comparison, and it
# was otherwise about to pay for a sync plus a full CI round.
#
# Fails CLOSED: an API error, an empty answer, a non-integer, a malformed merge
# base, or a truncated file list all read as "not current", because `behind`'s
# remedy (fleet.sh sync) is always safe and a false `ready` is not.
branch_is_current() {
  local ref_line base head_oid slug cmp behind merge_base cmp_rest health
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
  # One call yields "<behind_by>|<merge base sha>". The merge base rides along
  # because the "what did main land?" range is `<merge base>...<base>` and
  # fetching it separately would be a second round trip for a value this
  # response already carries.
  cmp="$(gh api "repos/$slug/compare/$base...$head_oid?per_page=1" \
    --jq '((.behind_by // "") | tostring) + "|" + (.merge_base_commit.sha // "")' \
    2>/dev/null)" || return 1
  # Split by FIELD COUNT for the same reason every other answer in this file is:
  # neither an integer nor a SHA can contain `|`, so a surplus field means the
  # answer is malformed and must fail closed rather than be seeked into.
  IFS='|' read -r behind merge_base cmp_rest <<<"$cmp"
  [[ -z "$cmp_rest" ]] || return 1
  [[ "$behind" =~ ^[0-9]+$ ]] || return 1
  if [[ "$behind" -eq 0 ]]; then return 0; fi
  [[ "$merge_base" =~ ^[0-9a-f]{7,40}$ ]] || return 1

  # THE #1157 PRECONDITION (#1159). Everything below this line is the relaxation
  # — the lane is behind, and `main_changes_are_inert` is about to decide it may
  # merge anyway. That decision is only sound while the `push: main` backstop is
  # alive, so ask before spending it. Placed HERE, and not one line lower, for
  # the ordering reason in the header: after the file listing, a lane behind a
  # lockfile bump would already have printed `behind` and been synced onto a
  # broken `main`. Placed here and not one line HIGHER so the two cheap
  # validations above still short-circuit without paying for the call — this is
  # one more API request per behind lane per wake, and the fleet is what runs out
  # of API budget first.
  #
  # `|| health=""` because the helper sits under `set -e`: an unguarded call that
  # exited non-zero would abort this script mid-classification, and the
  # orchestrator's contract reads a non-zero exit with empty stdout as a TOOLING
  # error — dispatching nothing and logging nothing useful. `--repo` is forwarded
  # or a `--repo`-scoped invocation would silently read some other repo's `main`.
  # The helper's own stderr (its attribution and blame range) is deliberately NOT
  # swallowed: it is the only place the operator learns WHICH commit is red.
  health=""
  if [[ -x "$MAIN_HEALTH_HELPER" ]]; then
    health="$("$MAIN_HEALTH_HELPER" ${repo_args[@]+"${repo_args[@]}"})" || health=""
  fi
  if [[ "$health" != "$MAIN_HEALTHY_TOKEN" ]]; then
    main_health_hold="${health:-unreadable}"
    return 1
  fi

  main_changes_are_inert "$slug" "$base" "$head_oid" "$merge_base"
}

# Fresh LGTM ⇔ latest verdict is LGTM AND its createdAt is strictly newer than
# the HEAD commit. RFC3339 UTC timestamps are fixed-width, so a lexical string
# compare is a correct chronological compare (portable — no date arithmetic).
# The guards COMPOSE rather than substitute: an inadmissible marker has already
# blanked both fields above (#1181), so a verdict must be attested AND fresh AND
# LGTM to reach `ready` — and an attested LGTM that predates HEAD is still stale.
# Absent that: a FRESH non-LGTM verdict is Gate 4 failed → `changes-requested`
# (checked FIRST — the verdict is the review gate speaking, so it outranks the
# no-gate shortcut); otherwise the lane waits for review, unless there is
# provably no review to wait for — and the review-gate probe is LAZY for the
# same rate-limit reason as the compare probe: only a lane already lacking a
# fresh verdict ever pays for it.
#
# The `changes-requested` test below is only HALF of "the verdict outranks the
# shortcut": it reads `verdict_lgtm`, which the provenance guard blanks, so it
# cannot speak for a REFUSED verdict. The other half is the latch inside
# `review_gate_absent`, which refuses the shortcut on the mere existence of a
# verdict comment. Both are needed, and a change that drops either one lets a
# refused verdict end at a token the loop merges on (#1181).
ready_token="ready"
if [[ "$verdict_lgtm" != "true" || -z "$verdict_date" ]] || ! [[ "$verdict_date" > "$head_date" ]]; then
  # Exactly `false` (jq's tostring), never `!= true`: anything else in that
  # field is a malformed answer, and the dispatch-a-worker token must fail
  # closed to the wait token below rather than fire on garbage.
  if [[ "$verdict_lgtm" == "false" && -n "$verdict_date" ]] && [[ "$verdict_date" > "$head_date" ]]; then
    echo "changes-requested"; exit 0
  fi
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
elif [[ -n "$main_health_hold" ]]; then
  # Not current, and the reason we cannot clear it is `main` itself. `behind`
  # would be the wrong answer here even though the branch IS behind: its remedy
  # is a sync, and a sync onto a red `main` imports the breakage, burns a CI
  # round, and re-reports as this lane's own `ci-failed`. Wait instead — and
  # `main-not-green` is in watch-pr.sh's IN_FLIGHT_TOKENS so waiting is what the
  # watcher does, rather than busy-waking the fleet (#1159).
  echo "main-not-green"
else
  # THE SECOND PRECONDITION (#1160). Reaching this `else` means "behind": the
  # remedy is `fleet.sh sync`, which advances HEAD and so spends this lane's
  # LGTM. Ask whether that verdict can be earned back before spending it.
  #
  # The guards are the header's four conditions, and the two written here are the
  # lanes with nothing to preserve or nothing to gain: `ready-unreviewed` has no
  # verdict to lose, and a non-CLEAN lane's ONLY remedy is the sync (holding it
  # would be a permanent wedge). They are also what keeps the probe LAZY — the
  # same rate-limit argument as every other probe in this file, and sharper here
  # because the question is literally "have we run out of API budget?".
  # `main-not-green` needs no guard of its own: it is the `elif` above, so a lane
  # already held never reaches this branch and never pays for the call.
  #
  # `|| quota=""` because the helper runs under `set -e`: an unguarded call that
  # exited non-zero would abort mid-classification, and the orchestrator reads a
  # non-zero exit with empty stdout as a TOOLING error, dispatching nothing. That
  # empty string then falls through to `behind`, which is the correct answer for
  # an unreadable one. `--repo` is forwarded so a `--repo`-scoped invocation does
  # not read some other repo's reviewer. The helper's own stderr is deliberately
  # NOT swallowed: it is the only place the operator learns which run proved the
  # exhaustion and when the window lifts.
  quota=""
  if [[ "$ready_token" == "ready" && "$merge_state" == "CLEAN" && -x "$REVIEW_QUOTA_HELPER" ]]; then
    quota="$("$REVIEW_QUOTA_HELPER" ${repo_args[@]+"${repo_args[@]}"})" || quota=""
  fi
  # Compared against the ONE token that holds. Everything else — `available`,
  # `unknown`, empty, a future token, a debug print — is not proof, and an
  # unproven hold costs days of a wedged lane (see the polarity block).
  if [[ "$quota" == "$REVIEW_QUOTA_EXHAUSTED_TOKEN" ]]; then
    echo "$REVIEW_QUOTA_HELD_TOKEN"
  else
    echo "behind"
  fi
fi
