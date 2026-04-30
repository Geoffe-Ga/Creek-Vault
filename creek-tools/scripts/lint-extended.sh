#!/usr/bin/env bash
# scripts/lint-extended.sh - Extended static analysis (CI-004 / STYLE-001)
#
# Status: OPTIONAL — not currently invoked from check-all.sh or CI.
# Pre-commit already runs refurb/tryceratops/vulture/etc. on staged
# files, and the existing whole-tree backlog (STYLE-001) prevents this
# script from being a hard gate today. Once STYLE-001 closes, wire
# this script into check-all.sh and the CI quality job.
#
# Until then, run it ad-hoc to triage the backlog:
#
#     ./scripts/lint-extended.sh
#
# Tools invoked (each optional at the binary level — skipped with a
# warning if not installed — but ENFORCED if available; no `|| true`):
#   - pylint (--fail-under per CI-002)
#   - refurb (modernisation hints; STYLE-001)
#   - tryceratops (exception-handling hygiene; STYLE-001)
#   - vulture (dead-code detection)
#   - interrogate (docstring coverage)
#   - shellcheck (shell scripts)
#   - detect-secrets (audit baseline)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

PYLINT_FAIL_UNDER="${PYLINT_FAIL_UNDER:-9.0}"
INTERROGATE_FAIL_UNDER="${INTERROGATE_FAIL_UNDER:-95}"

VERBOSE=false
while [[ $# -gt 0 ]]; do
    case $1 in
        --verbose) VERBOSE=true; shift ;;
        --help)
            cat <<EOF
Usage: $(basename "$0") [--verbose]

Extended static-analysis suite. Honours these env vars:
  PYLINT_FAIL_UNDER       (default 9.0)
  INTERROGATE_FAIL_UNDER  (default 95)
EOF
            exit 0
            ;;
        *) echo "Error: unknown option: $1" >&2; exit 2 ;;
    esac
done

cd "$PROJECT_ROOT"
if $VERBOSE; then set -x; fi

FAILED=()
SKIPPED=()

run_or_skip() {
    local name="$1"
    local bin="$2"
    shift 2
    if ! command -v "$bin" >/dev/null 2>&1; then
        echo "  ⚠ $name: $bin not installed; skipping"
        SKIPPED+=("$name")
        return 0
    fi
    if "$@"; then
        echo "  ✓ $name passed"
    else
        echo "  ✗ $name failed" >&2
        FAILED+=("$name")
    fi
}

echo "=== Extended Lint Suite ==="

run_or_skip "pylint"      pylint        \
    pylint creek/ --fail-under="$PYLINT_FAIL_UNDER"
run_or_skip "refurb"      refurb        \
    refurb creek/
run_or_skip "tryceratops" tryceratops   \
    tryceratops creek/
run_or_skip "vulture"     vulture       \
    vulture creek/ --min-confidence 80
run_or_skip "interrogate" interrogate   \
    interrogate -vv --fail-under="$INTERROGATE_FAIL_UNDER" creek/
run_or_skip "shellcheck"  shellcheck    \
    bash -c 'shellcheck scripts/*.sh'

if command -v detect-secrets >/dev/null 2>&1 && [[ -f .secrets.baseline ]]; then
    if detect-secrets audit --report --fail-on-unaudited .secrets.baseline \
        >/dev/null 2>&1; then
        echo "  ✓ detect-secrets passed"
    else
        echo "  ⚠ detect-secrets baseline has unaudited entries"
        # Audit gating is non-fatal until baseline is groomed; the
        # detect-secrets pre-commit hook still blocks new secrets.
    fi
else
    echo "  ⚠ detect-secrets: tool or baseline missing; skipping"
    SKIPPED+=("detect-secrets")
fi

echo ""
if [[ ${#SKIPPED[@]} -gt 0 ]]; then
    echo "Skipped (tool not installed):"
    for s in "${SKIPPED[@]}"; do echo "  - $s"; done
fi

if [[ ${#FAILED[@]} -gt 0 ]]; then
    echo "Failed:"
    for f in "${FAILED[@]}"; do echo "  ✗ $f"; done
    exit 1
fi

echo "✓ Extended lint suite passed"
