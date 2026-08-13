#!/usr/bin/env bash
# scripts/lint-vulture.sh - Dead-code detection (vulture) for creek/ + creek_mcp/.
#
# Issue #1395. Before this script the repo had a vulture "gate" that could
# not fail: every invocation passed `--min-confidence 80`, but vulture
# scores an unused function/method/class/property/attribute at 60%, so the
# entire dead-symbol tier was invisible. A brand-new zero-caller function
# added to creek/ produced zero findings. Nothing ran it either — the only
# two call sites were a pre-commit hook (CI runs no pre-commit step) and
# scripts/lint-extended.sh (not wired into check-all.sh or CI).
#
# The threshold is NOT a single number, because no single number works:
# at 80 the dead-symbol tier vanishes, and at 60 the tree reports 287
# findings. scripts/lint_vulture.py applies a per-type confidence floor
# instead, scans tests/ as a reference source, and carves out symbols the
# language or a framework invokes implicitly. That reaches zero findings
# with zero allowlist entries — see that module's docstring for the policy
# and for the four things this gate deliberately cannot see.
#
# This wrapper is the ONE definition of the gate. check-all.sh, CI,
# lint-extended.sh and the pre-commit hook all route through it so the
# policy cannot drift back apart. It takes no positional arguments by
# design: every *gate* call site therefore scans the same fixed scope,
# and none of them can narrow it into a green run.
#
# The module underneath does accept paths, deliberately — ad-hoc triage
# (a production-only pass, or a sweep of crawdad/) needs them. That is
# not a hole, because no gate reaches the module except through here.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

# Parse the standard --verbose / --help flags so the helper in
# check-all.sh can forward them uniformly.
while [[ $# -gt 0 ]]; do
    case $1 in
        --verbose) shift ;;  # the policy module has no verbose mode worth toggling
        --help)
            cat <<EOF
Usage: $(basename "$0")

Run the dead-code gate (vulture, per-type confidence floors) over
creek/ and creek_mcp/, using tests/ as a reference source.

Takes no positional arguments — the scan scope is fixed in
scripts/lint_vulture.py so it cannot be narrowed at a call site.

EXIT CODES:
    0   No dead code found
    3   Dead code found (delete it; do not add an allowlist entry)
    2   Unknown option
EOF
            exit 0
            ;;
        *) echo "Error: unknown option: $1" >&2; exit 2 ;;
    esac
done

cd "$PROJECT_ROOT"

# `python -m` (not a direct path) so `scripts.lint_vulture` resolves as a
# PEP-420 namespace package from PROJECT_ROOT — the same mechanism that
# lets tests do `from scripts.lint_vulture import ...` with no __init__.py.
exec python -m scripts.lint_vulture
