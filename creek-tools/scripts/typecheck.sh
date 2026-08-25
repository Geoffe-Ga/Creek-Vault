#!/usr/bin/env bash
# scripts/typecheck.sh - Run type checking with MyPy
# Usage: ./scripts/typecheck.sh [--fast|--incremental] [--verbose] [--help]

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

VERBOSE=false
# Cold by default: see the CACHING section of --help below, and issue #1186.
INCREMENTAL=false

# Parse command line arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        --verbose)
            VERBOSE=true
            shift
            ;;
        --fast|--incremental)
            INCREMENTAL=true
            shift
            ;;
        --help)
            cat << EOF
Usage: $(basename "$0") [OPTIONS]

Run type checking on the project using MyPy.

OPTIONS:
    --verbose       Show detailed output
    --fast          Reuse mypy's incremental cache instead of the cold default.
                    --incremental is an alias. Faster while iterating; see
                    CACHING for why it is not what gates a push.
    --help          Display this help message

CACHING:
    The default run passes --no-incremental, so mypy always reads live source.

    Mypy validates a cache entry on (mtime, size) and trusts it when both
    match. An edit that preserves both -- cp -p, tar -x, rsync -t, a
    same-length change, a branch switch onto a file of identical length -- is
    therefore answered out of .mypy_cache, and this gate reports clean on a
    tree CI rejects (issue #1186). Ruff's gate reached the same conclusion in
    issue #1119 and passes --no-cache unconditionally.

    Cold costs roughly 40s on this tree. That is the price of a local gate
    whose verdict means what CI's means; --fast is there for the edit-check
    loop, not for the last check before pushing.

EXIT CODES:
    0               All type checks passed
    1               Type errors found
    2               Error running type checker

EXAMPLES:
    $(basename "$0")            # Cold run: the gate
    $(basename "$0") --fast     # Cached run: iterating on a file
    $(basename "$0") --verbose  # Show detailed output
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

echo "=== Type Checking (MyPy) ==="

# Use python -m mypy so the same interpreter that has the project
# dependencies installed runs the type checker. This avoids
# "Cannot find implementation" noise when mypy is installed via
# pipx/uv into a different site-packages.
#
# Both first-party packages are checked: creek/ (pipeline + CLI) and
# creek_mcp/ (MCP server, auth, token policy, path confinement).
# creek_mcp/ was outside the target list until issue #925 — keep the
# two of them together here and in .github/workflows/ci.yml.
#
# scripts/ joined the list in issue #1395, which added
# scripts/lint_vulture.py. Fixing an ungated gate by adding a second
# ungated one would be no fix at all, so the dead-code policy module
# is type-checked (and, via pyproject's coverage source, covered)
# exactly like product code.
#
# The two invocations are spelled out rather than assembled from an array
# because an empty array expanded under `set -u` is an error on bash 3.2,
# which is still what /bin/bash is on macOS.
run_mypy() {
    if $INCREMENTAL; then
        python -m mypy creek/ creek_mcp/ scripts/
    else
        python -m mypy --no-incremental creek/ creek_mcp/ scripts/
    fi
}

if command -v mypy &> /dev/null; then
    run_mypy || {
        echo "✗ Type checking failed" >&2
        exit 1
    }
    echo "✓ Type checking passed"
else
    echo "Warning: mypy not installed, skipping type checking" >&2
    echo "Install with: pip install mypy" >&2
    exit 0
fi

exit 0
