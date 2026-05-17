#!/usr/bin/env bash
# scripts/lint-refurb.sh - Refurb (modernisation hints) for creek/.
#
# STYLE-001 closed the backlog (148 → 0); this gate keeps it at zero.
# Any new violation will block CI. The `# noqa: FURB…` escape hatch is
# available for the documented carve-outs (see creek-tools/CLAUDE.md
# §6.1 for the STYLE-001 status + carve-out list).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

# Parse the standard --verbose / --help flags so the helper in
# check-all.sh can forward them uniformly.
while [[ $# -gt 0 ]]; do
    case $1 in
        --verbose) shift ;;  # refurb has no verbose mode worth toggling
        --help)
            cat <<EOF
Usage: $(basename "$0") [--verbose]

Run refurb (modernisation hints) against creek/. Exits non-zero on any
violation; STYLE-001 holds the backlog at zero.
EOF
            exit 0
            ;;
        *) echo "Error: unknown option: $1" >&2; exit 2 ;;
    esac
done

cd "$PROJECT_ROOT"
exec refurb creek/
