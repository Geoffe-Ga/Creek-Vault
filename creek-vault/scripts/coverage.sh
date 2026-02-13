#!/usr/bin/env bash
# scripts/coverage.sh - Run tests with coverage report
# Usage: ./scripts/coverage.sh [--html] [--xml] [--verbose] [--help]

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

HTML_REPORT=false
XML_REPORT=false
VERBOSE=false

# Parse command line arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        --html)
            HTML_REPORT=true
            shift
            ;;
        --xml)
            XML_REPORT=true
            shift
            ;;
        --verbose)
            VERBOSE=true
            shift
            ;;
        --help)
            cat << EOF
Usage: $(basename "$0") [OPTIONS]

Run tests with coverage report.

OPTIONS:
    --html      Generate HTML coverage report
    --xml       Generate XML coverage report (for CI)
    --verbose   Show detailed output
    --help      Display this help message

EXIT CODES:
    0           Coverage threshold met
    1           Coverage below threshold
    2           Error running coverage

EXAMPLES:
    $(basename "$0")          # Run coverage with terminal report
    $(basename "$0") --html   # Generate HTML report
    $(basename "$0") --xml    # Generate XML report for CI
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

echo "=== Coverage Report ==="

# Build pytest arguments
PYTEST_ARGS=(
    -v
    --cov=creek_vault
    --cov-branch
    --cov-report=term-missing
    --cov-fail-under=90
)

# Add HTML report if requested
if $HTML_REPORT; then
    PYTEST_ARGS+=(--cov-report=html)
    echo "HTML report will be generated in htmlcov/"
fi

# Add XML report if requested
if $XML_REPORT; then
    PYTEST_ARGS+=(--cov-report=xml)
    echo "XML report will be generated as coverage.xml"
fi

# Run tests with coverage
pytest "${PYTEST_ARGS[@]}" tests/ || {
    echo "✗ Coverage below threshold" >&2
    exit 1
}

echo "✓ Coverage threshold met"

exit 0
