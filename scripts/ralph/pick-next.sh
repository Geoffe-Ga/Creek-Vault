#!/usr/bin/env bash
# scripts/ralph/pick-next.sh
#
# Ralph's picker for creek-tools (Geoffe-Ga/Creek-Vault). Prints the
# lowest-numbered open issue that is a real, unblocked, not-already-in-flight
# implementation issue — or nothing if the backlog is drained.
#
# creek-tools does NOT use PillBreakfast's `spec-decomposition` label, so this
# picker is label-configurable. Tune via env vars:
#
#   RALPH_REQUIRE_LABELS  Space-separated labels an issue MUST have (ALL of
#                         them). Empty (default) = no required label; any open
#                         issue is a candidate.
#   RALPH_EXCLUDE_LABELS  Space-separated labels that DISQUALIFY an issue.
#                         Default excludes housekeeping / deferred / in-flight
#                         markers. Override to taste.
#
# Eligibility:
#   - open
#   - has every label in RALPH_REQUIRE_LABELS (if any)
#   - has none of the labels in RALPH_EXCLUDE_LABELS
#   - not already addressed by an open PR (`Closes|Fixes|Resolves #N` in body)
#
# Exit codes:
#   0 — issue number printed (or nothing if backlog empty / drained)
#   2 — gh CLI not authenticated / missing
set -euo pipefail

if ! command -v gh >/dev/null 2>&1; then
  echo "pick-next: gh CLI not found" >&2
  exit 2
fi

REQUIRE_LABELS="${RALPH_REQUIRE_LABELS:-}"
EXCLUDE_LABELS="${RALPH_EXCLUDE_LABELS:-epic wontfix duplicate invalid question blocked needs-spec future-work do-not-auto-merge in-progress}"

# jq array literals from the space-separated env vars.
require_json=$(printf '%s\n' $REQUIRE_LABELS | jq -R . | jq -s .)
exclude_json=$(printf '%s\n' $EXCLUDE_LABELS | jq -R . | jq -s .)

# Open candidates, ascending by number, filtered by require/exclude labels.
candidates=$(
  gh issue list \
    --state open \
    --limit 300 \
    --json number,title,labels \
    --jq "
      ( $require_json | map(select(length>0)) ) as \$req
      | ( $exclude_json | map(select(length>0)) ) as \$exc
      | sort_by(.number)
      | map(. as \$i | (\$i.labels | map(.name)) as \$names
          | select(
              ( \$req | all(. as \$r | \$names | index(\$r)) )
              and ( \$exc | any(. as \$x | \$names | index(\$x)) | not )
            ))
      | .[].number
    "
)

if [[ -z "$candidates" ]]; then
  exit 0
fi

# Issue numbers already addressed by an open PR (case-insensitive Closes/Fixes/Resolves #N).
inflight=$(
  gh pr list \
    --state open \
    --limit 300 \
    --json number,body \
    --jq '.[].body' \
  | grep -oiE '(closes|fixes|resolves)[[:space:]]+#[0-9]+' \
  | grep -oE '[0-9]+' \
  | sort -u || true
)

# Print the lowest candidate not already in flight.
while IFS= read -r n; do
  [[ -z "$n" ]] && continue
  if [[ -z "$inflight" ]] || ! grep -qx "$n" <<<"$inflight"; then
    echo "$n"
    exit 0
  fi
done <<<"$candidates"

# All eligible issues already have PRs in flight, or backlog drained.
exit 0
