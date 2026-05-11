#!/usr/bin/env bash
# scripts/state-budget.sh - Verify the creek state report stays within budget.
#
# FEAT-007: ``00-Creek-Meta/State/latest.md`` is the session-start context for
# CrawDad and Claude Code, and must fit in a single Claude context window. The
# gate is enforced at 50,000 tokens (~200KB).
#
# Usage:
#   ./scripts/state-budget.sh [--vault PATH] [--help]
#
# A failing budget is a fragmentation signal, not a cap to raise — see
# docs/generation.md for the rationale.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

VAULT_PATH="${CREEK_VAULT:-}"

while [[ $# -gt 0 ]]; do
    case $1 in
        --vault)
            VAULT_PATH="$2"
            shift 2
            ;;
        --help)
            cat << EOF
Usage: $(basename "$0") [OPTIONS]

Verify 00-Creek-Meta/State/latest.md is under the 50,000-token budget.

OPTIONS:
    --vault PATH   Vault root (default: \$CREEK_VAULT)
    --help         Show this help message

EXIT CODES:
    0   Within budget (or no report present)
    1   Budget exceeded
    2   Argument or invocation error
EOF
            exit 0
            ;;
        --verbose)
            # No verbose output yet; accept the flag for parity with sibling
            # scripts so check-all.sh's --verbose forwarding does not break.
            shift
            ;;
        *)
            echo "Error: Unknown option: $1" >&2
            exit 2
            ;;
    esac
done

if [[ -z "$VAULT_PATH" ]]; then
    echo "state-budget: no vault path provided (set CREEK_VAULT or pass --vault); skipping."
    exit 0
fi

LATEST="$VAULT_PATH/00-Creek-Meta/State/latest.md"
if [[ ! -f "$LATEST" ]]; then
    echo "state-budget: $LATEST not present; skipping."
    exit 0
fi

cd "$PROJECT_ROOT"
python -m creek.generate.state_budget "$LATEST"
