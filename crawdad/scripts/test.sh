#!/usr/bin/env bash
# scripts/test.sh — Pytest with branch coverage gate.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$(dirname "$SCRIPT_DIR")"

pytest "$@"
