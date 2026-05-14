#!/usr/bin/env bash
# scripts/check-all.sh — Run every quality gate for crawdad.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_ROOT"

echo "=== crawdad quality checks ==="
echo

FAILED=()
PASSED=()

run() {
    local name="$1"; shift
    echo "→ $name"
    if "$@"; then
        PASSED+=("$name")
        echo "  ✓ $name passed"
    else
        FAILED+=("$name")
        echo "  ✗ $name failed" >&2
    fi
    echo
}

run "Lint"             "$SCRIPT_DIR/lint.sh"
run "Format"           "$SCRIPT_DIR/format.sh" --check
run "Type check"       "$SCRIPT_DIR/typecheck.sh"
run "Security"         "$SCRIPT_DIR/security.sh"
run "Docstrings"       "$SCRIPT_DIR/docstrings.sh"
run "Complexity"       "$SCRIPT_DIR/complexity.sh"
run "Tests + coverage" "$SCRIPT_DIR/test.sh"

echo "=== Summary ==="
echo "Passed: ${#PASSED[@]}"
echo "Failed: ${#FAILED[@]}"
if (( ${#FAILED[@]} > 0 )); then
    echo
    echo "Failed checks:"
    for name in "${FAILED[@]}"; do
        echo "  ✗ $name"
    done
    exit 1
fi
echo
echo "✓ All checks passed."
