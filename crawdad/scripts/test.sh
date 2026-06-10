#!/usr/bin/env bash
# scripts/test.sh — Pytest with branch coverage gate.
#
# With no arguments, integration tests (live API smokes) are deselected so
# the default run and CI never make — or bill — a real vendor call. Pass
# pytest args explicitly to opt in, e.g.:
#   ./scripts/test.sh -m integration --no-cov -k openai
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$(dirname "$SCRIPT_DIR")"

if [[ $# -eq 0 ]]; then
    set -- -m "not integration"
fi
pytest "$@"
