#!/usr/bin/env bash
# scripts/pylint.sh - Run Pylint with the project's --fail-under threshold.
#
# This is the single command both ./scripts/check-all.sh and CI invoke
# so a developer who passes the local gate cannot fail the CI gate
# (and vice-versa). The PYLINT_FAIL_UNDER env var lets CI override the
# default; absent that, we use 9.0 to match CLAUDE.md.
#
# Usage: ./scripts/pylint.sh [--json PATH] [--verbose]

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

FAIL_UNDER="${PYLINT_FAIL_UNDER:-9.0}"

# Pylint's --py-version defaults to the interpreter running it, so the
# version-dependent checks used to mean whichever matrix leg happened to
# execute. Pinning the project's SUPPORTED FLOOR makes the result the same
# everywhere and targets the oldest interpreter we ship on — which is the
# strict reading, and the one that matches ruff's `target-version = "py311"`
# in pyproject.toml. Raise this only when requires-python does (issue #1141).
PY_VERSION_FLOOR="${PYLINT_PY_VERSION:-3.11}"

# Pylint's own process parallelism. 0 = one worker per core. Measured
# identical message sets at -j 0/2/4 and serial on this tree (issue #1141),
# so this buys wall time without changing the verdict.
JOBS="${PYLINT_JOBS:-0}"

JSON_OUTPUT=""
VERBOSE=false

while [[ $# -gt 0 ]]; do
    case $1 in
        --json)
            JSON_OUTPUT="$2"
            shift 2
            ;;
        --verbose)
            VERBOSE=true
            shift
            ;;
        --help)
            cat <<EOF
Usage: $(basename "$0") [--json PATH] [--verbose]

Run Pylint with --fail-under=\$PYLINT_FAIL_UNDER (default 9.0).

OPTIONS:
    --json PATH   Also write a JSON snapshot to PATH (does not gate)
    --verbose     Show detailed output
    --help        Display this help message

EXIT CODES:
    0   Score >= threshold
    1   Score below threshold (build should fail)
    2   Pylint failed to run
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

if $VERBOSE; then set -x; fi

if ! command -v pylint >/dev/null 2>&1; then
    echo "✗ pylint not installed (try: pip install -e '.[dev]')" >&2
    exit 2
fi

echo "=== Pylint (--fail-under=$FAIL_UNDER, --py-version=$PY_VERSION_FLOOR) ==="

# ONE analysis pass, always. Until issue #1141 this script ran the whole
# codebase through pylint TWICE whenever `--json` was passed — once to
# gate, once more to write an informational snapshot nobody gates on —
# and CI passes `--json`. That second pass was 167 of the step's 334 CI
# seconds, on the critical path, on all three matrix legs. Pylint emits
# several formats from a single run (`json:PATH,colorized`), so the
# artifact costs nothing extra and the gate is unchanged.
#
# Anti-regression: `tests/test_scanner_coverage.py` asserts there is
# exactly ONE `python -m pylint` invocation here. Adding a second
# "just for the artifact" is the defect this comment exists to prevent.
OUTPUT_FORMAT="colorized"
if [[ -n "$JSON_OUTPUT" ]]; then
    mkdir -p "$(dirname "$JSON_OUTPUT")"
    OUTPUT_FORMAT="json:${JSON_OUTPUT},colorized"
fi

# Run via `python -m pylint` so the same interpreter that has the project
# deps installed runs the linter — same hygiene applied to typecheck.sh
# and test.sh.
#
# Targets both first-party packages: creek/ (pipeline + CLI) and
# creek_mcp/ (MCP server, auth, token policy, path confinement), which
# was outside the target list until issue #925.
#
# Kept on ONE physical line: the gate-contract tests read this file
# line-by-line, so splitting the targets away from --fail-under would
# make those assertions pass vacuously.
python -m pylint creek/ creek_mcp/ -j "$JOBS" --py-version="$PY_VERSION_FLOOR" --output-format="$OUTPUT_FORMAT" --fail-under="$FAIL_UNDER"

echo "✓ Pylint score >= $FAIL_UNDER"
