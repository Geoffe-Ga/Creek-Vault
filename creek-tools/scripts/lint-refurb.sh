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

# --python-version is pinned to the project's SUPPORTED FLOOR rather than left
# to default to whichever interpreter is running. Refurb suggests newer idioms
# as the target version rises, so an unpinned run on 3.13 can demand a rewrite
# that does not parse on 3.11 — the oldest version `requires-python` promises.
# Pinning also makes this gate interpreter-independent, which is what lets CI
# run it once instead of once per matrix leg (issue #1141). It matches ruff's
# `target-version = "py311"`; raise both together when requires-python moves.
exec refurb creek/ --python-version "${REFURB_PY_VERSION:-3.11}"
