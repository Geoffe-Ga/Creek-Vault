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

echo "=== Pylint (--fail-under=$FAIL_UNDER) ==="

# Gating run. Run via `python -m pylint` so the same interpreter that
# has the project deps installed runs the linter — same hygiene
# applied to typecheck.sh and test.sh.
python -m pylint creek/ --output-format=colorized --fail-under="$FAIL_UNDER"

# Optional JSON snapshot (informational artifact only). The gating
# decision was already made by the previous command; if pylint exits
# non-zero here it's a JSON-formatter-only failure and is reported
# via stderr but not propagated.
if [[ -n "$JSON_OUTPUT" ]]; then
    mkdir -p "$(dirname "$JSON_OUTPUT")"
    if ! python -m pylint creek/ --output-format=json > "$JSON_OUTPUT" 2>/dev/null; then
        echo "  ⚠ pylint JSON snapshot failed; see $JSON_OUTPUT" >&2
    fi
fi

echo "✓ Pylint score >= $FAIL_UNDER"
