#!/usr/bin/env bash
# scripts/bench.sh - Run the model-capacity harness (python -m creek_mcp.bench).
# Usage: ./scripts/bench.sh reflect [--mode fake|live] [--provider ollama|pool]
#            --out REPORT.json [...]
#        ./scripts/bench.sh cost --price-file PRICES.json --out COST.json [...]
#        ./scripts/bench.sh cost --price-file PRICES.json \
#            --metered-model MODEL_ID --token-report REPORT.json \
#            [--metered-currency USD|EUR] [--monthly-cap N] \
#            [--tokens-spent-input N] [--tokens-spent-output N] --out COST.json
#        ./scripts/bench.sh --help
#
# Hermetic by default (--mode fake), for both providers: --provider pool runs
# the shared confidential inference pool's client against an in-process fake
# pool, so no vendor is contacted and nothing is billed (ADR-0016 Decision 4,
# issue #1892). A live run drives an operator-supplied Ollama or pool and must
# be reproducible from its report, so this wrapper injects the checkout's
# commit as --git-sha when a live `reflect` run did not pass one.
# `cost` with --metered-model prices ADR-0016's per-token pool instead of a Fly
# Machine-month, from the *measured* token block in a benchmark report. Only a
# `provider_reported` basis is citable: a fake run's byte bound and an absent
# measurement are refused (exit 2) rather than estimated (#1897).
# Exit codes are the harness's own: 0 ok, 2 refused, 1 failed (issue #1850).
# See docs/bench.md.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

# shellcheck source-path=SCRIPTDIR
# shellcheck source=_lib.sh
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
