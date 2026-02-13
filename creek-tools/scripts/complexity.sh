#!/usr/bin/env bash
# scripts/complexity.sh - Code complexity analysis
# Usage: ./scripts/complexity.sh [--verbose] [--help]

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

Analyze code complexity using Radon and Xenon.

Metrics:
  - Cyclomatic complexity (should be <= 10)
  - Maintainability index (should be >= 20)
  - Cognitive complexity

OPTIONS:
    --verbose   Show detailed output
    --help      Display this help message

EXIT CODES:
    0           Complexity acceptable
    1           Complexity exceeds thresholds
    2           Error during analysis

EXAMPLES:
    $(basename "$0")          # Analyze complexity
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

echo "=== Code Complexity Analysis ==="

# Check Cyclomatic Complexity with Radon
if command -v radon &> /dev/null; then
    echo ""
    echo "Cyclomatic Complexity (should be <= 10):"
    radon cc -a start_green_stay_green/ || true

    echo ""
    echo "Maintainability Index (should be >= 20):"
    radon mi -a start_green_stay_green/ || true
else
    echo "Warning: radon not installed, skipping cyclomatic complexity check" >&2
fi

# Check complexity with Xenon
if command -v xenon &> /dev/null; then
    if $VERBOSE; then
        echo "Running Xenon complexity check..."
    fi
    xenon --max-absolute B --max-modules B --max-average B start_green_stay_green/ ||         { echo "✗ Complexity exceeds thresholds" >&2; exit 1; }
else
    if $VERBOSE; then
        echo "Note: xenon not installed for strict complexity checks"
    fi
fi

echo "✓ Complexity analysis completed"
exit 0
