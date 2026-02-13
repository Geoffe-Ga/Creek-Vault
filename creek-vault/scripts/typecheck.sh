#!/usr/bin/env bash
# scripts/typecheck.sh - Run type checking with MyPy
# Usage: ./scripts/typecheck.sh [--verbose] [--help]

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

VERBOSE=false

# Parse command line arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        --verbose)
            VERBOSE=true
            shift
            ;;
        --help)
            cat << EOF
Usage: $(basename "$0") [OPTIONS]

Run type checking on the project using MyPy.

OPTIONS:
    --verbose   Show detailed output
    --help      Display this help message

EXIT CODES:
    0           All type checks passed
    1           Type errors found
    2           Error running type checker

EXAMPLES:
    $(basename "$0")          # Run type checking
    $(basename "$0") --verbose # Show detailed output
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

if command -v mypy &> /dev/null; then
    mypy creek_vault/ || {
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
