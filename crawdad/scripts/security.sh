#!/usr/bin/env bash
# scripts/security.sh — Bandit on the source tree.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$(dirname "$SCRIPT_DIR")"

bandit -r crawdad -ll
