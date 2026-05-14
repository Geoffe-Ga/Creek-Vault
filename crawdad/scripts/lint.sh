#!/usr/bin/env bash
# scripts/lint.sh — Ruff lint. Pass --fix to auto-correct.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$(dirname "$SCRIPT_DIR")"

FIX=""
for arg in "$@"; do
    case "$arg" in
        --fix) FIX="--fix" ;;
    esac
done

ruff check $FIX crawdad tests
