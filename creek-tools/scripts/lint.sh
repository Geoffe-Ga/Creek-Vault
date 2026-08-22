#!/usr/bin/env bash
# scripts/lint.sh - Run linting checks with Ruff
# Usage: ./scripts/lint.sh [--fix] [--check] [--verbose] [--help]

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

FIX=false
CHECK=false
VERBOSE=false

# Parse command line arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        --fix)
            FIX=true
            shift
            ;;
        --check)
            CHECK=true
            shift
            ;;
        --verbose)
            VERBOSE=true
            shift
            ;;
        --help)
            cat << EOF
Usage: $(basename "$0") [OPTIONS]

Run linting checks on the project using Ruff.

OPTIONS:
    --fix       Auto-fix linting issues where possible
    --check     Check only, fail if issues found (default mode)
    --verbose   Show detailed output
    --help      Display this help message

EXIT CODES:
    0           All checks passed
    1           Linting issues found
    2           Error running checks

EXAMPLES:
    $(basename "$0")              # Run checks in check mode
    $(basename "$0") --fix         # Auto-fix issues
    $(basename "$0") --verbose     # Show detailed output
EOF
            exit 0
            ;;
        *)
            echo "Error: Unknown option: $1" >&2
            exit 2
            ;;
    esac
done

cd "$PROJECT_ROOT"

# Set verbosity
if $VERBOSE; then
    set -x
fi

echo "=== Linting (Ruff) ==="

# The --no-cache below is deliberate and load-bearing (issue #1119).
# Ruff keys its per-file cache on mtime alone -- no size, no content hash --
# so any content change that leaves the mtime unchanged or restored poisons
# it: cp -p, rsync -t, tar -x, a checkout of an older tree, an
# mtime-preserving editor. CI never has a .ruff_cache and always re-reads the
# file, so a local gate that answers from one can clear a tree CI rejects.
# That is not hypothetical: it cost a real CI failure on PR #1117.
# Running cold measures at ~0.05s over this tree (477 files) -- negligible --
# so do not drop the flag to speed the gate up. tests/test_ruff_cache_poisoning.py
# and tests/test_ruff_gate_parity.py fail if you do.

# `set -e` (line 5) already owns the failure path: a non-zero ruff exits the
# script immediately, with ruff's own status, which is what honours the three
# exit codes documented above -- 2 ("Error running checks") stays 2 rather
# than collapsing into 1. What `set -e` cannot do is say which gate spoke, and
# an `EXIT_CODE=$?` capture cannot run after a command that already killed the
# script, so the report that used to follow was unreachable (issue #1189).
# An ERR trap is the one shape that adds the announcement without taking the
# exit code away -- and without moving `ruff check` into an `if` condition,
# where tests/test_ruff_gate_parity.py's `^ruff\s` scan would stop seeing it
# and the --no-cache and no-autofix gates would silently assert over nothing.
_announce_lint_failure() {
    local rc=$?
    echo "✗ Linting checks failed" >&2
    exit "$rc"
}
trap _announce_lint_failure ERR

if $FIX; then
    if $VERBOSE; then
        echo "Fixing linting issues..."
    fi
    ruff check . --fix --no-cache
else
    if $VERBOSE; then
        echo "Checking for linting issues..."
    fi
    ruff check . --no-cache
fi

trap - ERR
echo "✓ Linting checks passed"
