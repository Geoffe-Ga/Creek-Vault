#!/usr/bin/env bash
# scripts/format.sh — Ruff format.
#   default: apply (write) — the "--fix" mode for Ruff format.
#   --check: dry-run, fail on diffs (CI-safe).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$(dirname "$SCRIPT_DIR")"

MODE=""
for arg in "$@"; do
    case "$arg" in
        --check) MODE="--check" ;;
    esac
done

ruff format $MODE crawdad tests
