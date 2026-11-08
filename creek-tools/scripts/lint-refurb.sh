#!/usr/bin/env bash
# scripts/lint-refurb.sh - Refurb (modernisation hints) for creek/ + creek_mcp/.
#
# STYLE-001 closed the creek/ backlog (148 → 0) and #965 closed creek_mcp/'s
# (60 → 0); this gate keeps both at zero. CI calls this script, as do
# pre-commit and lint-extended.sh, so it is the single definition.
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

Run refurb (modernisation hints) against creek/ and creek_mcp/. Exits
non-zero on any violation in either package.
EOF
            exit 0
            ;;
        *) echo "Error: unknown option: $1" >&2; exit 2 ;;
    esac
done

# Both packages in one run (#925, #965). Refurb's verdict is a finding count
# with a ceiling of zero, so unlike interrogate's percentage a combined run
# cannot let one package's cleanliness mask the other's findings.
REFURB_GATED_PACKAGES=(creek/ creek_mcp/)

cd "$PROJECT_ROOT"

# --python-version is pinned to the project's SUPPORTED FLOOR rather than left
# to default to whichever interpreter is running. Refurb suggests newer idioms
# as the target version rises, so an unpinned run on 3.13 can demand a rewrite
# that does not parse on 3.11 — the oldest version `requires-python` promises.
# Pinning also makes this gate interpreter-independent, which is what lets CI
# run it once instead of once per matrix leg (issue #1141). It matches ruff's
# `target-version = "py311"`; raise both together when requires-python moves.
exec refurb "${REFURB_GATED_PACKAGES[@]}" --python-version "${REFURB_PY_VERSION:-3.11}"
