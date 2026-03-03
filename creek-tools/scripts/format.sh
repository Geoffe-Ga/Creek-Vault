#!/usr/bin/env bash
# scripts/format.sh - Format code with Ruff
# Usage: ./scripts/format.sh [--fix] [--check] [--verbose] [--help]

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

Format code using Ruff (formatter + import sorting).

OPTIONS:
    --fix       Apply formatting changes (default)
    --check     Check only, fail if changes needed
    --verbose   Show detailed output
    --help      Display this help message

EXIT CODES:
    0           Code is properly formatted
    1           Formatting issues found
    2           Error running checks

EXAMPLES:
    $(basename "$0") --fix         # Apply formatting
    $(basename "$0") --check       # Check only
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

echo "=== Formatting (Ruff) ==="

if $CHECK; then
    # Check import sorting
    if $VERBOSE; then
        echo "Checking import sorting..."
    fi
    ruff check --select I . || { echo "✗ Import sorting check failed" >&2; exit 1; }

    # Check code formatting
    if $VERBOSE; then
        echo "Checking code formatting..."
    fi
    ruff format --check . || { echo "✗ Code formatting check failed" >&2; exit 1; }

    echo "✓ Code formatting check passed"
else
    # Fix import sorting
    if $VERBOSE; then
        echo "Sorting imports..."
    fi
    ruff check --select I --fix . || { echo "✗ Import sorting failed" >&2; exit 1; }

    # Format code
    if $VERBOSE; then
        echo "Formatting code..."
    fi
    ruff format . || { echo "✗ Code formatting failed" >&2; exit 1; }

    echo "✓ Code formatted successfully"
fi
exit 0
