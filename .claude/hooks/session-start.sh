#!/bin/bash
set -euo pipefail

# Only run in remote (web) environments
if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
  exit 0
fi

PROJECT_DIR="${CLAUDE_PROJECT_DIR:-/home/user/project}"

echo "Installing creek-tools dependencies (uv, locked)..."
cd "$PROJECT_DIR/creek-tools"
if ! command -v uv >/dev/null 2>&1; then
  python3 -m pip install -q uv
fi
uv sync --all-extras

echo "Installing pre-commit hooks..."
uv run pre-commit install
uv run pre-commit install --hook-type commit-msg
uv run pre-commit install --hook-type pre-push

echo "Session setup complete."
