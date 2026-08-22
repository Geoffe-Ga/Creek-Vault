#!/usr/bin/env bash
# scripts/test.sh - Run tests with Pytest
# Usage: ./scripts/test.sh [--unit|--integration|--e2e|--live|--all] [--coverage]
#                          [-k EXPRESSION] [--verbose] [--help]

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

# How many lanes to assume are running when nothing says otherwise. This is
# `fleet.sh`'s own documented default for `max_workers`, restated rather than
# read, because `scripts/ralph/` is repo-root tooling and this script must keep
# working in a checkout that has none. An explicit CREEK_TEST_WORKERS is the
# override for a fleet configured differently.
readonly ASSUMED_CONCURRENT_LANES=4

# Workers one lane may claim when it has not been given an explicit budget:
# the machine's cores divided among the lanes assumed to be sharing it, floored
# at 1. Deriving it rather than hardcoding is the point — a fixed 2 still
# oversubscribes a 4-core box 2x at four lanes, which is the same defect #1554
# was filed to remove, only smaller. Guessing high OOMs every lane at once;
# guessing low only costs time, so this rounds down.
creek_lane_workers() {
    local cores
    cores="$(getconf _NPROCESSORS_ONLN 2>/dev/null || echo 1)"
    [[ "$cores" =~ ^[0-9]+$ ]] && (( cores > 0 )) || cores=1
    local slice=$(( cores / ASSUMED_CONCURRENT_LANES ))
    (( slice < 1 )) && slice=1
    printf '%s' "$slice"
}

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

ENVIRONMENT:
    CREEK_TEST_WORKERS  pytest-xdist worker count for the unit lane.
                        Default 'auto' (one per core) for a normal checkout
                        and for CI, which own the machine. Inside a Ralph
                        fleet lane (a checkout under .ralph/worktrees/) the
                        default is instead the machine's cores divided among
                        the lanes assumed to share it — max(1, nproc / 4),
                        so 1 on a 4-core box and 3 on a 12-core one. A slice
                        of 1 means serial, i.e. no -n at all. Every
                        concurrent lane runs this script, so 'auto' there
                        would claim every core in each of them — 'lanes x
                        ncpu' workers, which exhausts memory (#1554). Set it
                        explicitly to give a lane an exact budget; set 1 (or
                        0) to run serially, needed for pdb, which xdist
                        swallows.

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

        # Distribute the unit lane across cores (pytest-xdist). This is the
        # only lane that gets it: it is ~8,300 tests and was 213 of the CI
        # job's seconds run serially on a 4-core runner with three cores idle
        # (issue #1141). Measured 83s -> 40s locally at 4 workers.
        #
        # Coverage is NOT weakened by this. pytest-cov collects per worker and
        # combines; the totals were verified byte-identical to the serial run
        # (26344 stmts / 1249 miss / 6912 branch / 591 partial / 94.18%) at
        # -n 4 and -n auto alike, so the 90% gate below means what it meant.
        #
        # CREEK_TEST_WORKERS=1 (or 0) forces serial for an interactive
        # debugging session — xdist swallows pdb and interleaves output.
        #
        # `auto` means one worker per core, which assumes THIS RUN OWNS THE
        # MACHINE. That holds for CI (one job per runner) and for a developer
        # running the suite once. It does NOT hold inside a Ralph fleet lane:
        # check-all.sh runs this script, and the fleet runs up to `max_workers`
        # lanes at once, so the real worker count is `lanes x ncpu` rather than
        # `ncpu`. On a 10-core box that was ~60 pytest processes at ~1 GB each
        # and it OOM'd the machine (#1554).
        #
        # So a lane — detected by its worktree path, since fleet.sh injects no
        # environment — takes a derived slice of the box rather than all of it,
        # and says so on stderr. An explicit CREEK_TEST_WORKERS always wins, which
        # is how an orchestrator that knows the real lane count sets an exact
        # budget.
        WORKERS="${CREEK_TEST_WORKERS:-}"
        if [[ -z "$WORKERS" ]]; then
            if [[ "$PROJECT_ROOT" == *"/.ralph/worktrees/"* ]]; then
                WORKERS="$(creek_lane_workers)"
                echo "NOTE: fleet lane detected; using -n $WORKERS instead of" \
                     "'auto' so concurrent lanes do not exhaust memory (#1554)." \
                     "Set CREEK_TEST_WORKERS to override." >&2
            else
                WORKERS="auto"
            fi
        fi
        if [[ "$WORKERS" != "0" && "$WORKERS" != "1" ]]; then
            PYTEST_ARGS+=(-n "$WORKERS")
        fi
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
