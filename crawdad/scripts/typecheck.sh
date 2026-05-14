#!/usr/bin/env bash
# scripts/typecheck.sh — MyPy in strict mode.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$(dirname "$SCRIPT_DIR")"

mypy crawdad
