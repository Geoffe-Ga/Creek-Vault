#!/usr/bin/env bash
# scripts/fix-all.sh - Auto-fix all issues
# Usage: ./scripts/fix-all.sh [--verbose] [--help]

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

Auto-fix all auto-fixable issues in sequence.

Fixes:
  1. Linting issues (Ruff)
  2. Formatting (Black + isort)

Note: Some issues may require manual intervention.
Check the output and review changes before committing.

OPTIONS:
    --verbose   Show detailed output
    --help      Display this help message

EXIT CODES:
    0           Fixes applied successfully
    1           Some fixes failed
    2           Error during fixes

EXAMPLES:
    $(basename "$0")          # Apply all auto-fixes
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
VERBOSE_FLAG=""
if $VERBOSE; then
    VERBOSE_FLAG="--verbose"
fi

echo "=== Auto-fixing Issues ==="
echo ""

FAILED_FIXES=()

# Helper function to run a fix
run_fix() {
    local fix_name=$1
    local script=$2
    shift 2
    local args=("$@")

    echo "Running: $fix_name"
    if "$SCRIPT_DIR/$script" --fix "${args[@]}" $VERBOSE_FLAG; then
        echo "✓ $fix_name completed"
    else
        FAILED_FIXES+=("$fix_name")
        echo "✗ $fix_name failed" >&2
    fi
    echo ""
}

# Run all fixes
run_fix "Linting" "lint.sh"
run_fix "Formatting" "format.sh"

echo "=== Auto-fix Summary ==="
if [ ${#FAILED_FIXES[@]} -gt 0 ]; then
    echo "Failed fixes: ${#FAILED_FIXES[@]}"
    echo ""
    for fix in "${FAILED_FIXES[@]}"; do
        echo "  ✗ $fix"
    done
    exit 1
else
    echo "✓ All auto-fixes completed successfully!"
    echo ""
    echo "Review the changes with: git diff"
    echo "Stage changes with: git add ."
    exit 0
fi
