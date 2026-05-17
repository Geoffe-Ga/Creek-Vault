#!/usr/bin/env bash
# scripts/lint-tryceratops.sh - Tryceratops (exception-handling hygiene)
# for creek/.
#
# STYLE-001 closed the backlog (21 → 0); this gate keeps it at zero.
# Any new violation will block CI. The `# noqa: TRY…` escape hatch is
# available for the documented carve-outs (separation of failure modes,
# ValueError contracts, etc. — see creek-tools/CLAUDE.md §6.1).

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

# tryceratops exits 0 even with findings. We grep stdout for the `[TRY`
# violation prefix as the failure signal. The grep is output-format-
# fragile, so a canary assertion guards against silent regression: the
# "Done processing!" footer must appear, otherwise we treat the run as
# broken (e.g. tryceratops crashed before reporting, or the output
# format changed upstream) and exit non-zero.
OUTPUT=$(tryceratops creek/ 2>&1)
echo "$OUTPUT"

if ! grep -q "Done processing" <<<"$OUTPUT"; then
    echo "ERROR: tryceratops did not print the 'Done processing' footer." >&2
    echo "       Output format may have changed; treating as failure." >&2
    exit 2
fi

if grep -qE "^\[TRY" <<<"$OUTPUT"; then
    exit 1
fi
exit 0
