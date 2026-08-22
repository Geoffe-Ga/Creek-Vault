#!/usr/bin/env bash
# scripts/ralph/pick-next.sh
#
# Ralph's picker. Prints the next open
# issue that is a real, unblocked, not-already-in-flight implementation issue
# AND is safe to start alongside whatever the fleet is already working — or
# nothing if the backlog is drained / nothing compatible remains.
#
# The picker is label-configurable. Tune via env vars:
#
#   RALPH_REQUIRE_LABELS  Space-separated labels an issue MUST have (ALL of
#                         them). Empty (default) = no required label; any open
#                         issue is a candidate.
#   RALPH_EXCLUDE_LABELS  Space-separated labels that DISQUALIFY an issue.
#                         Default excludes housekeeping / deferred / in-flight
#                         markers. Override to taste.
#   RALPH_SOLO_LABEL      Label marking an issue that must run ALONE (never in
#                         parallel with any other worker). Default: "solo".
#   RALPH_PARALLEL_LABEL  Label that overrides the same-epic guard so two issues
#                         under one epic may still run in parallel. Default:
#                         "parallelizable".
#   RALPH_RESPECT_EPICS   When "1" (default), a candidate that shares an
#                         epic-prefixed label with an active issue is skipped
#                         (likely ordered/overlapping) unless it carries the
#                         parallel label. Set "0" to disable the guard.
#   RALPH_DEFAULT_PRIORITY_RANK  Priority tier (0=P0 … 3=P3) assumed for an
#                         issue carrying no P-label. Default 1 (== P1). See the
#                         priority-tiering block below.
#
# Priority ordering: candidates are walked by [priority tier, number ascending],
# oldest-first within a tier. Tier 0 = P0 / priority-critical … tier 3 = P3 /
# priority-low (see the tiering block below — both label vocabularies are honored).
#
# Parallel awareness (see scripts/ralph/FLEET.md):
#   Issues already being worked — either an open PR (`Closes|Fixes|Resolves #N`)
#   or a live worktree under .ralph/worktrees/issue-<N> — are excluded. Among
#   what remains, the FIRST worker (empty active set) gets the lowest eligible
#   issue as before. Additional workers only get an issue that is *independent*
#   of every active issue: not `solo`, and (unless `parallelizable`) not sharing
#   an epic label with an active issue. A `solo` issue, once active, blocks any
#   further parallel pick. Correctness across imperfect independence guesses is
#   guaranteed at merge time by the orchestrator's serialized-merge + sync.
#
# Exit codes:
#   0 — issue number printed (or nothing if backlog empty / nothing compatible)
#   2 — gh CLI not authenticated / missing
#
# Requires bash >= 4 (associative arrays `declare -A`, `${var,,}` lowercasing);
# on macOS use /opt/homebrew/bin/bash (bash 5), not the system bash 3.2.
set -euo pipefail

if ! command -v gh >/dev/null 2>&1; then
  echo "pick-next: gh CLI not found" >&2
  exit 2
fi

REQUIRE_LABELS="${RALPH_REQUIRE_LABELS:-}"
EXCLUDE_LABELS="${RALPH_EXCLUDE_LABELS:-epic wontfix duplicate invalid question blocked needs-spec future-work do-not-auto-merge in-progress}"
SOLO_LABEL="${RALPH_SOLO_LABEL:-solo}"
PARALLEL_LABEL="${RALPH_PARALLEL_LABEL:-parallelizable}"
RESPECT_EPICS="${RALPH_RESPECT_EPICS:-1}"

# Priority tiering. Candidates are ordered by priority tier first, then
# oldest-first WITHIN a tier: tier 0 (critical/breakage) preempts tier 1
# (bugs + feature issues) preempts tier 2 (quality) preempts tier 3 (hygiene).
#
# TWO label vocabularies map onto the same four tiers, so the picker honors both
# the repo's long-standing `priority-*` labels AND the maintenance pipeline's
# P0–P3 labels:
#   tier 0  ← `P0` or `priority-critical`
#   tier 1  ← `P1` or `priority-high`
#   tier 2  ← `P2` or `priority-medium`
#   tier 3  ← `P3` or `priority-low`
# An issue with none of these sorts at RALPH_DEFAULT_PRIORITY_RANK.
#
#   RALPH_DEFAULT_PRIORITY_RANK  Tier an unlabeled issue is treated as. Default
#                                1 (== P1), so legacy/unlabeled feature work
#                                keeps flowing at feature priority and only P2/P3
#                                scan hygiene sorts behind it. With no P-labels
#                                anywhere, every issue ranks equal and ordering
#                                collapses to the previous oldest-first behavior
#                                — this change is backward compatible.
#
# #1011 asked whether this default should move off P1, since untriaged issues
# were reaching the front of the queue unjudged (two groom gates, ten issues,
# eventual triage spanning P1..P3 — wrong more often than right, in BOTH
# directions). The default stays at 1, deliberately:
#
#   * No fixed default is defensible. The evidence shows the correct tier for an
#     untriaged issue is genuinely unknown, so any constant is a guess.
#   * Between the two guesses, P1 fails safer. Ranking untriaged work last would
#     let a genuinely urgent finding sit unseen indefinitely; ranking it first
#     costs, at worst, some queue order that a human can re-triage.
#   * The real defect was the *silence*, not the number — a rank asserted with no
#     signal that it was assumed. So the fix is at the two ends instead: labels
#     are mandatory at the point of filing (PROMPT.md step 8), and the walk below
#     announces on stderr whenever it falls back to this default.
#
# Once labelling holds at the source the default should rarely be consulted at
# all, which is the point: it is a backstop, not a policy.
#
# To enforce the pipeline's stricter "agent-ready required" gate (so ONLY fully
# specified scan/feature issues are picked), set RALPH_REQUIRE_LABELS=agent-ready.
# It is left empty by default to avoid starving an existing unlabeled backlog.
DEFAULT_RANK="${RALPH_DEFAULT_PRIORITY_RANK:-1}"
if ! printf '%s' "$DEFAULT_RANK" | grep -qE '^[0-9]+$'; then
  DEFAULT_RANK=1
fi

# jq array literals from the space-separated env vars.
require_json=$(printf '%s\n' $REQUIRE_LABELS | jq -R . | jq -s .)
exclude_json=$(printf '%s\n' $EXCLUDE_LABELS | jq -R . | jq -s .)

# All open issues as "<number>\t<comma-separated-labels>", filtered by
# require/exclude labels and ordered by [priority-tier, number ascending].
# Fetched once; reused for candidates and for looking up active issues' labels.
open_tsv=$(
  gh issue list \
    --state open \
    --limit 300 \
    --json number,labels \
    --jq "
      ( $require_json | map(select(length>0)) ) as \$req
      | ( $exclude_json | map(select(length>0)) ) as \$exc
      | map(. as \$i | (\$i.labels | map(.name)) as \$names
          | select(
              ( \$req | all(. as \$r | \$names | index(\$r)) )
              and ( \$exc | any(. as \$x | \$names | index(\$x)) | not )
            )
          | { number: \$i.number, names: \$names,
              rank: ( if   ((\$names | index(\"P0\")) or (\$names | index(\"priority-critical\"))) then 0
                      elif ((\$names | index(\"P1\")) or (\$names | index(\"priority-high\")))     then 1
                      elif ((\$names | index(\"P2\")) or (\$names | index(\"priority-medium\")))   then 2
                      elif ((\$names | index(\"P3\")) or (\$names | index(\"priority-low\")))      then 3
                      else $DEFAULT_RANK end ) })
      | sort_by([.rank, .number])
      | .[]
      | \"\(.number)\t\(.names | join(\",\"))\"
    "
)

# Full label map (unfiltered) so we can inspect the labels of active issues even
# if they carry an excluded label (e.g. an in-flight issue with `in-progress`).
labels_of() {
  local n="$1"
  gh issue view "$n" --json labels --jq '[.labels[].name] | join(",")' 2>/dev/null || true
}

if [[ -z "$open_tsv" ]]; then
  exit 0
fi

# Issue numbers already in flight via an open PR (case-insensitive markers).
inflight=$(
  gh pr list \
    --state open \
    --limit 300 \
    --json body \
    --jq '.[].body' \
  | grep -oiE '(closes|fixes|resolves)[[:space:]]+#[0-9]+' \
  | grep -oE '[0-9]+' \
  | sort -u || true
)

# Issue numbers with a live worktree (started, PR not yet opened).
worktree_issues=""
if repo_root=$(git rev-parse --show-toplevel 2>/dev/null); then
  wt_dir="$repo_root/.ralph/worktrees"
  if [[ -d "$wt_dir" ]]; then
    worktree_issues=$(
      find "$wt_dir" -maxdepth 1 -type d -name 'issue-*' 2>/dev/null \
        | sed 's#^.*/issue-##' | sort -u || true
    )
  fi
fi

# The active set = in-flight PR issues ∪ worktree issues.
active=$(printf '%s\n%s\n' "$inflight" "$worktree_issues" | grep -E '^[0-9]+$' | sort -u || true)

is_active() { [[ -n "$active" ]] && grep -qx "$1" <<<"$active"; }

# Pre-fetch each active issue's labels ONCE. conflicts_with_active() runs per
# candidate and consults active-issue labels in both the solo and epic loops;
# without this cache that would be up to 2×(active workers) `gh issue view` calls
# per candidate. Keyed by issue number.
declare -A ACTIVE_LABELS=()
if [[ -n "$active" ]]; then
  while IFS= read -r _a; do
    [[ -n "$_a" ]] || continue
    ACTIVE_LABELS["$_a"]="$(labels_of "$_a")"
  done <<<"$active"
fi

# Exact per-token membership: has_label "<labels-csv>" "<label>" (case-insensitive).
# Matches a whole comma-separated label, NOT a substring or the joined line — so
# a `solo` guard fires on "bug,solo,backend", not only on a lone "solo" label.
has_label() {
  local want="${2,,}" tok
  local -a toks
  IFS=',' read -ra toks <<<"$1"
  for tok in "${toks[@]}"; do
    [[ "${tok,,}" == "$want" ]] && return 0
  done
  return 1
}

# Epic-prefixed labels of an issue (labels beginning with "epic"). Lowercased
# BEFORE `sort -u` so the same-epic guard's `comm -12` intersection is
# case-insensitive (e.g. "Epic-Foo" matches "epic-foo") and collates correctly.
epic_labels() {
  printf '%s\n' "${1//,/$'\n'}" | grep -iE '^epic' | tr '[:upper:]' '[:lower:]' | sort -u || true
}

# Both label vocabularies that count as a deliberate triage decision (#1011).
_PRIORITY_LABELS="P0 P1 P2 P3 priority-critical priority-high priority-medium priority-low"

# Has this candidate been triaged at all? Delegates to has_label, so matching is
# per whole comma-separated token — a near-miss like `P1x` is not triage.
has_priority_label() {
  local labels="$1" p
  for p in $_PRIORITY_LABELS; do
    has_label "$labels" "$p" && return 0
  done
  return 1
}

# Does the candidate (labels CSV) conflict with any active issue?
conflicts_with_active() {
  local cand_labels="$1"
  [[ -n "$active" ]] || return 1 # first worker: never conflicts

  # A candidate that must run solo cannot join a non-empty fleet.
  if has_label "$cand_labels" "$SOLO_LABEL"; then
    return 0
  fi

  # If any active issue is solo, it monopolizes the fleet.
  local a a_labels
  while IFS= read -r a; do
    [[ -n "$a" ]] || continue
    a_labels="${ACTIVE_LABELS[$a]:-}"
    if has_label "$a_labels" "$SOLO_LABEL"; then
      return 0
    fi
  done <<<"$active"

  # Same-epic guard (unless the candidate opts into parallel).
  if [[ "$RESPECT_EPICS" == "1" ]] \
    && ! has_label "$cand_labels" "$PARALLEL_LABEL"; then
    local cand_epics
    cand_epics="$(epic_labels "$cand_labels")"
    if [[ -n "$cand_epics" ]]; then
      while IFS= read -r a; do
        [[ -n "$a" ]] || continue
        local a_epics
        a_epics="$(epic_labels "${ACTIVE_LABELS[$a]:-}")"
        [[ -n "$a_epics" ]] || continue
        if comm -12 <(printf '%s\n' "$cand_epics") <(printf '%s\n' "$a_epics") \
          | grep -q .; then
          return 0
        fi
      done <<<"$active"
    fi
  fi

  return 1
}

# Walk candidates ascending; print the first that is neither active nor
# conflicting with the active set.
while IFS=$'\t' read -r n cand_labels; do
  [[ -z "$n" ]] && continue
  is_active "$n" && continue
  if conflicts_with_active "$cand_labels"; then
    continue
  fi
  # Rank the untriaged, but never silently (#1011). An unlabeled issue sorts at
  # DEFAULT_RANK, which two groom gates showed was wrong more often than right
  # (ten issues; eventual triage spanned P1..P3, in both directions). The rank
  # is left alone — ranking untriaged work last would bury a genuinely urgent
  # finding, the worse of the two failures — so the fix is to stop the silence.
  # stderr only: stdout is the orchestrator's parse target and stays bare.
  if ! has_priority_label "$cand_labels"; then
    printf 'pick-next: issue #%s has no priority label; ranking it at the default tier P%s. Triage it, or set RALPH_DEFAULT_PRIORITY_RANK.\n' \
      "$n" "$DEFAULT_RANK" >&2
  fi
  echo "$n"
  exit 0
done <<<"$open_tsv"

# Backlog drained, or nothing compatible with the current fleet remains.
exit 0
