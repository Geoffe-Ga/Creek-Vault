#!/usr/bin/env bash
# scripts/lint-vulture.sh — Dead-code detection (vulture) for crawdad/.
#
# Issue #1472. crawdad was outside the dead-code gate entirely: #1395 built
# the policy in creek-tools and wired it into creek-tools' check-all.sh and
# CI, and this project's check-all.sh ran seven gates, none of them vulture.
# A zero-caller function added under crawdad/ was reported by nothing.
#
# This wrapper does NOT re-implement the policy — it executes creek-tools'
# scripts/lint_vulture.py, which declares both subprojects' scan surfaces
# side by side as `Scope` values. That is the whole point: a copy of a
# 400-line policy is four call sites that will eventually disagree about a
# threshold, which is the drift #1395's single-wrapper design exists to
# prevent. Because this script imports that module rather than duplicating
# it, crawdad's CI job is also the drift alarm: a policy change that breaks
# this project reddens it immediately.
#
# Two facts make sharing across two virtualenvs tractable:
#   * Vulture never imports the code it scans — it is pure `ast.parse` — so
#     crawdad's dependencies do not have to be importable from creek-tools,
#     and creek-tools' do not have to be importable from here.
#   * The only thing each environment must provide is a `vulture`
#     distribution. It is declared in this project's [dev] extra and pinned
#     in crawdad/uv.lock, which is what CI installs from.
#
# Like its creek-tools twin this takes no positional arguments by design:
# the scan surface is fixed in the policy module, so no gate call site can
# narrow it into a green run.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
REPO_ROOT="$(dirname "$PROJECT_ROOT")"
POLICY_ROOT="$REPO_ROOT/creek-tools"

while [[ $# -gt 0 ]]; do
    case $1 in
        --verbose) shift ;;  # the policy module has no verbose mode worth toggling
        --help)
            cat <<EOF
Usage: $(basename "$0")

Run the shared dead-code gate (vulture, per-type confidence floors) over
crawdad/, using crawdad/tests/ as a reference source. The policy lives in
creek-tools/scripts/lint_vulture.py and is shared, not copied.

Takes no positional arguments — the scan scope is fixed in that module so
it cannot be narrowed at a call site.

EXIT CODES:
    0   No dead code found
    3   Dead code found (delete it; do not add an allowlist entry)
    2   Unknown option, or the shared policy module is missing
EOF
            exit 0
            ;;
        *) echo "Error: unknown option: $1" >&2; exit 2 ;;
    esac
done

if [[ ! -f "$POLICY_ROOT/scripts/lint_vulture.py" ]]; then
    echo "Error: shared dead-code policy not found at $POLICY_ROOT/scripts/lint_vulture.py" >&2
    echo "This gate runs creek-tools' policy module; the sibling checkout must be present." >&2
    exit 2
fi

cd "$POLICY_ROOT"

# `python -m` (not a direct path) so `scripts.lint_vulture` resolves as a
# PEP-420 namespace package from POLICY_ROOT, exactly as creek-tools' own
# wrapper does. `--scope crawdad` selects this project's Scope.
exec python -m scripts.lint_vulture --scope crawdad
