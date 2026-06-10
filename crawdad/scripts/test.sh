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
# Inject --no-cov when explicitly targeting integration tests: the
# project-wide --cov-fail-under=90 would otherwise trip on a handful of
# live smokes and bury their output under a coverage failure. The negated
# default selection ("not integration") must keep its coverage gate.
for arg in "$@"; do
    if [[ "$arg" == *integration* && "$arg" != *"not integration"* ]]; then
        set -- "$@" --no-cov
        break
    fi
done
pytest "$@"
