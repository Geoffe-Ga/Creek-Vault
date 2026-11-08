#!/usr/bin/env bash
# scripts/lint-interrogate.sh - Docstring coverage gate for creek/ + creek_mcp/.
#
# Pins docstring coverage at the CLAUDE.md §6.1 threshold (≥95%), held by
# each package on its own. CI calls this script — see
# .github/workflows/ci.yml "Check docstring coverage" step — as do
# pre-commit and lint-extended.sh, so a local pass means a CI pass on this
# gate (GAP-007 parity).

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

Run interrogate against creek/ and creek_mcp/, holding each package to
\$DOCSTRING_COVERAGE_THRESHOLD (default: 95) independently. Fails when
either package falls below it.
EOF
            exit 0
            ;;
        *) echo "Error: unknown option: $1" >&2; exit 2 ;;
    esac
done

# One interrogate run per package (#925, #965), never one combined run. A
# combined `interrogate creek/ creek_mcp/` reports a single aggregate, and
# creek/'s ~3,800 nodes at 99.6% would let creek_mcp/ (~2,500 nodes) sink to
# about 88% before that aggregate dipped under 95% — the free pass #965 closes.
DOCSTRING_GATED_PACKAGES=(creek/ creek_mcp/)

cd "$PROJECT_ROOT"

status=0
failed=()
for package in "${DOCSTRING_GATED_PACKAGES[@]}"; do
    echo "=== Docstring coverage: $package ==="
    package_status=0
    interrogate -vv --fail-under="$DOCSTRING_COVERAGE_THRESHOLD" "$package" || package_status=$?
    if [[ $package_status -ne 0 ]]; then
        status=$package_status
        failed+=("$package")
    fi
done

if [[ ${#failed[@]} -gt 0 ]]; then
    echo "✗ Docstring coverage below ${DOCSTRING_COVERAGE_THRESHOLD}% in: ${failed[*]}" >&2
fi
exit "$status"
