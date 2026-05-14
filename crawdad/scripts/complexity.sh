#!/usr/bin/env bash
# scripts/complexity.sh — Xenon cyclomatic-complexity gate.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$(dirname "$SCRIPT_DIR")"

xenon --max-absolute B --max-modules B --max-average A crawdad
