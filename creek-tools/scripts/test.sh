#!/usr/bin/env bash
# scripts/test.sh - Run tests with Pytest
# Usage: ./scripts/test.sh [--unit|--integration|--e2e|--live|--all] [--coverage]
#                          [-k EXPRESSION] [--verbose] [--help]

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

# shellcheck source=scripts/_lib.sh
source "$SCRIPT_DIR/_lib.sh"

TEST_TYPE="unit"
COVERAGE=false
VERBOSE=false
KEYWORD=""

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
        --live)
            TEST_TYPE="live"
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
        -k)
            if [[ $# -lt 2 ]]; then
                echo "Error: -k requires an expression argument" >&2
                exit 2
            fi
            KEYWORD="$2"
            shift 2
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
    --unit          Run unit tests only (default) — everything unmarked.
                    Carries the 90% coverage gate.
    --integration   Run the hermetic cross-component lane. No network, no API
                    keys, no real vault. BLOCKING in CI. Coverage gate off.
    --e2e           Run the hermetic end-to-end lane: full journeys through the
                    CLI against a synthetic vault. BLOCKING in CI. Coverage
                    gate off.
    --live          Run the live smokes: real provider APIs and local services.
                    Needs credentials; NOT run in CI. Each test skips cleanly
                    when its key or service is absent. Coverage gate off.
    --all           Run every test type, live smokes included
    --coverage      Generate coverage report
    -k EXPRESSION   Only run tests matching the pytest keyword expression
    --verbose       Show detailed output
    --help          Display this help message

WHICH LANE BLOCKS A MERGE:
    --unit, --integration and --e2e all gate the Quality Gate in
    .github/workflows/ci.yml. --live and the 'slow' benchmarks do not: CI holds
    no provider credentials, and a gate that depends on a paid third-party API
    or a local daemon is a flaky gate.

EXIT CODES:
    0               All tests passed
    1               Test failures
    2               Error running tests

EXAMPLES:
    $(basename "$0")                     # Run unit tests
    $(basename "$0") --all               # Run all tests
    $(basename "$0") --unit --coverage   # Unit tests with coverage
    $(basename "$0") --live -k openai    # Smoke one provider's live API
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
        # Every lane marker is excluded by name. `live` in particular: those
        # tests skip themselves when a key is absent, so omitting it here would
        # look fine in CI and quietly bill a real API on a developer's machine.
        PYTEST_ARGS+=(-m "not integration and not e2e and not slow and not live")
        ;;
    integration)
        echo "=== Running Integration Tests (hermetic, blocking) ==="
        # Hermetic cross-component tests: no network, no API keys, no real
        # vault. --no-cov because a marker-only selection exercises a fraction
        # of the package, so the project-wide --cov-fail-under from pyproject
        # addopts would always fail. Coverage is the unit lane's job; this lane
        # is about wiring between components.
        PYTEST_ARGS+=(-m "integration" --no-cov)
        ;;
    e2e)
        echo "=== Running End-to-End Tests (hermetic, blocking) ==="
        # Full journeys through the CLI against a synthetic vault. --no-cov for
        # the same reason as the integration lane above.
        PYTEST_ARGS+=(-m "e2e" --no-cov)
        ;;
    live)
        echo "=== Running Live Smokes (needs credentials; not run in CI) ==="
        # Real provider APIs and local services. Never wired into CI: it holds
        # no provider credentials, and each test skips when its key or service
        # is missing — a lane that skips everything is not a gate. --no-cov for
        # the same reason as the lanes above.
        PYTEST_ARGS+=(-m "live" --no-cov)
        ;;
    all)
        echo "=== Running All Tests ==="
        # Deliberately no -m expression: "all" means every marker, live smokes
        # and slow benchmarks included. Any new lane marker is picked up here
        # for free, which is the point — do not turn this into a union of the
        # lanes above, or a future marker silently stops being covered.
        ;;
esac

if [[ -n "$KEYWORD" ]]; then
    PYTEST_ARGS+=(-k "$KEYWORD")
fi

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
