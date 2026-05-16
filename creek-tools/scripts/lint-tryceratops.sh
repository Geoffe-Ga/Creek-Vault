#!/usr/bin/env bash
# scripts/lint-tryceratops.sh - Tryceratops (exception-handling hygiene)
# for creek/.
#
# STYLE-001 closed the backlog (21 → 0); this gate keeps it at zero.
# Any new violation will block CI. The `# noqa: TRY…` escape hatch is
# available for the documented carve-outs (separation of failure modes,
# ValueError contracts, etc. — see creek-tools/CLAUDE.md §6.2).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

while [[ $# -gt 0 ]]; do
    case $1 in
        --verbose) shift ;;
        --help)
            cat <<EOF
Usage: $(basename "$0") [--verbose]

Run tryceratops (exception-handling hygiene) against creek/. Exits
non-zero on any violation.
EOF
            exit 0
            ;;
        *) echo "Error: unknown option: $1" >&2; exit 2 ;;
    esac
done

cd "$PROJECT_ROOT"

# tryceratops emits findings even on exit-0 unless every line is empty;
# wrap the call so a zero-violation run actually exits 0 cleanly.
OUTPUT=$(tryceratops creek/ 2>&1)
echo "$OUTPUT"
if echo "$OUTPUT" | grep -qE "^\[TRY"; then
    exit 1
fi
exit 0
