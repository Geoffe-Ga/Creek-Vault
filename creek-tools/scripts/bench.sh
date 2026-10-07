#!/usr/bin/env bash
# scripts/bench.sh - Run the model-capacity harness (python -m creek_mcp.bench).
# Usage: ./scripts/bench.sh reflect [--mode fake|live] --out REPORT.json [...]
#        ./scripts/bench.sh cost --price-file PRICES.json --out COST.json [...]
#        ./scripts/bench.sh --help
#
# Hermetic by default (--mode fake). A live run drives an operator-supplied
# Ollama and must be reproducible from its report, so this wrapper injects the
# checkout's commit as --git-sha when a live `reflect` run did not pass one.
# Exit codes are the harness's own: 0 ok, 2 refused, 1 failed (issue #1850).
# See docs/bench.md.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

# shellcheck source=scripts/_lib.sh
source "$SCRIPT_DIR/_lib.sh"

cd "$PROJECT_ROOT"
creek_require_python_toolchain pydantic || exit 2

args=("$@")
is_live_reflect=false
has_git_sha=false
if [[ "${1:-}" == "reflect" ]]; then
    for ((i = 1; i < ${#args[@]}; i++)); do
        case "${args[$i]}" in
            --git-sha|--git-sha=*) has_git_sha=true ;;
            --mode=live) is_live_reflect=true ;;
            --mode)
                if [[ "${args[$((i + 1))]:-}" == "live" ]]; then
                    is_live_reflect=true
                fi
                ;;
        esac
    done
fi

if $is_live_reflect && ! $has_git_sha; then
    if ! sha="$(git rev-parse HEAD)"; then
        echo "refused: --git-sha is required for --mode live (not a git checkout)" >&2
        exit 2
    fi
    args+=(--git-sha "$sha")
fi

exec python -m creek_mcp.bench "${args[@]}"
