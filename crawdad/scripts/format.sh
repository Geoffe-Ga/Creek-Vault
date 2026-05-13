#!/usr/bin/env bash
# scripts/format.sh — Ruff format. Pass --check for a non-mutating check.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$(dirname "$SCRIPT_DIR")"

MODE=""
for arg in "$@"; do
    case "$arg" in
        --check) MODE="--check" ;;
        --fix)   MODE="" ;;
    esac
done

ruff format $MODE crawdad tests
