#!/usr/bin/env bash
# scripts/docstrings.sh — Interrogate docstring-coverage gate.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$(dirname "$SCRIPT_DIR")"

interrogate crawdad
