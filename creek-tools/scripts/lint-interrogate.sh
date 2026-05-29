#!/usr/bin/env bash
# scripts/lint-interrogate.sh - Docstring coverage gate for creek/.
#
# Pins docstring coverage at the CLAUDE.md §6.1 threshold (≥95%). The
# CI workflow runs the same command verbatim — see
# .github/workflows/ci.yml "Check docstring coverage" step — so a
# local pass means a CI pass on this gate (GAP-007 parity).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

DOCSTRING_COVERAGE_THRESHOLD="${DOCSTRING_COVERAGE_THRESHOLD:-95}"

while [[ $# -gt 0 ]]; do
    case $1 in
        --verbose) shift ;;
        --help)
            cat <<EOF
Usage: $(basename "$0") [--verbose]

Run interrogate against creek/ and fail when docstring coverage falls
below \$DOCSTRING_COVERAGE_THRESHOLD (default: 95).
EOF
            exit 0
            ;;
        *) echo "Error: unknown option: $1" >&2; exit 2 ;;
    esac
done

cd "$PROJECT_ROOT"
exec interrogate -vv --fail-under="$DOCSTRING_COVERAGE_THRESHOLD" creek/
