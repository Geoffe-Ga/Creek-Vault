#!/usr/bin/env bash
# scripts/test.sh - Run tests with Pytest
# Usage: ./scripts/test.sh [--unit|--integration|--e2e|--all] [--coverage]
#                          [--verbose] [--help]

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

# shellcheck source=scripts/_lib.sh
source "$SCRIPT_DIR/_lib.sh"

TEST_TYPE="unit"
COVERAGE=false
VERBOSE=false

# Parse command line arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        --unit)
            TEST_TYPE="unit"
            shift
            ;;
        --integration)
            TEST_TYPE="integration"
            shift
            ;;
        --e2e)
            TEST_TYPE="e2e"
            shift
            ;;
        --all)
            TEST_TYPE="all"
            shift
            ;;
        --coverage)
            COVERAGE=true
            shift
            ;;
        --verbose)
            VERBOSE=true
            shift
            ;;
        --help)
            cat << EOF
Usage: $(basename "$0") [OPTIONS]

Run tests using Pytest.

OPTIONS:
    --unit          Run unit tests only (default)
    --integration   Run integration tests only
    --e2e           Run end-to-end tests only
    --all           Run all test types
    --coverage      Generate coverage report
    --verbose       Show detailed output
    --help          Display this help message

EXIT CODES:
    0               All tests passed
    1               Test failures
    2               Error running tests

EXAMPLES:
    $(basename "$0")                     # Run unit tests
    $(basename "$0") --all               # Run all tests
    $(basename "$0") --unit --coverage   # Unit tests with coverage
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

# Fail fast with an actionable message if the Python toolchain is missing
# (fresh checkout, never ran dev-setup) rather than an opaque ImportError.
creek_require_python_toolchain || exit 2

# Set verbosity
if $VERBOSE; then
    set -x
fi

# Build pytest arguments
PYTEST_ARGS=(-v)

case "$TEST_TYPE" in
    unit)
        echo "=== Running Unit Tests ==="
        PYTEST_ARGS+=(-m "not integration and not e2e and not slow")
        ;;
    integration)
        echo "=== Running Integration Tests ==="
        PYTEST_ARGS+=(-m "integration")
        ;;
    e2e)
        echo "=== Running End-to-End Tests ==="
        PYTEST_ARGS+=(-m "e2e")
        ;;
    all)
        echo "=== Running All Tests ==="
        ;;
esac

# Add coverage if requested
if $COVERAGE; then
    echo "Coverage enabled"
    # Always emit a junit.xml alongside coverage so CI's test-results
    # artifact upload (.github/workflows/ci.yml) gets a real file
    # instead of silently uploading nothing.
    mkdir -p reports
    PYTEST_ARGS+=(
        --cov=creek
        --cov-branch
        --cov-report=term-missing
        --cov-report=html
        --cov-report=xml
        --cov-fail-under=90
        --junitxml=reports/junit.xml
        # CI-bail: a widespread regression (e.g. import error) would
        # otherwise burn through the full 2700+ test suite before
        # surfacing. Bail at 10 to balance visibility (multiple
        # failure sites) against wasted minutes on cascading errors.
        --maxfail=10
    )
fi

# Run tests
if $VERBOSE; then
    echo "Running pytest with args: ${PYTEST_ARGS[*]}"
fi

python -m pytest "${PYTEST_ARGS[@]}" tests/ \
    || { echo "✗ Tests failed" >&2; exit 1; }

echo "✓ Tests passed"

exit 0
