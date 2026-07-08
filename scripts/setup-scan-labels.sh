#!/usr/bin/env bash
# scripts/setup-scan-labels.sh
#
# Create (or update) the labels the autonomous maintenance pipeline relies on:
# the priority tiers consumed by scripts/ralph/pick-next.sh, the `agent-ready`
# gate, and one `scan:<name>` provenance label per scan type. Run once per repo;
# safe to re-run — `--force` makes each label create-or-update, so this is
# idempotent and can double as drift correction.
#
# Requires an authenticated `gh` CLI with `repo` scope. Usage:
#   ./scripts/setup-scan-labels.sh
set -euo pipefail

if ! command -v gh >/dev/null 2>&1; then
  echo "setup-scan-labels: gh CLI not found" >&2
  exit 2
fi

# label <name> <color> <description>
label() {
  gh label create "$1" --color "$2" --description "$3" --force
}

echo "Priority tiers (consumed by pick-next.sh: P0 -> P1 -> P2 -> P3)…"
label "P0" "B60205" "Security/breakage — preempts all work"
label "P1" "D93F0B" "Bugs + Geoff feature issues"
label "P2" "FBCA04" "Quality: refactor, coverage, perf"
label "P3" "0E8A16" "Hygiene: dead code, types, docs, TODOs"

echo "Consumption gate…"
label "agent-ready" "1D76DB" "Fully specified; Ralph may pick up"

echo "Scan provenance labels…"
for scan in deps security bugs dead-code complexity coverage perf todo \
            types mutation docs; do
  label "scan:${scan}" "C5DEF5" "Filed by the ${scan} maintenance scan"
done

echo "Done. Labels created/updated."
