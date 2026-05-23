#!/usr/bin/env bash
# scripts/dev-setup.sh - Provision a CI-equivalent local dev environment.
#
# Installs the fully-pinned uv.lock (or falls back to pip + .[dev]) and
# installs pre-commit hooks. After this completes, ./scripts/check-all.sh
# should produce the same result locally as the GitHub Actions CI run on
# the same commit (issue #206).
#
# Usage: ./scripts/dev-setup.sh [--no-precommit] [--pip] [--help]

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

INSTALL_PRECOMMIT=true
FORCE_PIP=false

while [[ $# -gt 0 ]]; do
    case $1 in
        --no-precommit)
            INSTALL_PRECOMMIT=false
            shift
            ;;
        --pip)
            FORCE_PIP=true
            shift
            ;;
        --help)
            cat << EOF
Usage: $(basename "$0") [OPTIONS]

Provision a CI-equivalent local dev environment for creek-tools.

OPTIONS:
    --no-precommit    Skip 'pre-commit install'.
    --pip             Use pip + 'pyproject.toml[dev]' instead of uv.
    --help            Display this help message.

By default, runs 'uv sync --all-extras' against uv.lock for a
reproducible install matching CI. Use --pip if you prefer a plain pip
install (slightly slower; unpinned but still self-contained because
[dev] includes [all]).

EXIT CODES:
    0    Setup completed
    1    Setup failed
EOF
            exit 0
            ;;
        *)
            echo "Error: Unknown option: $1" >&2
            exit 1
            ;;
    esac
done

cd "$PROJECT_ROOT"

echo "=== creek-tools dev setup ==="
echo ""

if $FORCE_PIP || ! command -v uv >/dev/null 2>&1; then
    if $FORCE_PIP; then
        echo "Using pip (--pip)."
    else
        echo "uv not found on PATH; falling back to pip."
        echo "Tip: install uv (https://docs.astral.sh/uv/) for a faster, locked install."
    fi
    echo "Installing creek-tools[dev] (includes [all] for test-time deps)..."
    python -m pip install --upgrade pip
    pip install -e '.[dev]'
else
    echo "Using uv (locked install from uv.lock)..."
    uv sync --all-extras
fi

if $INSTALL_PRECOMMIT; then
    echo ""
    echo "Installing pre-commit hooks..."
    if command -v pre-commit >/dev/null 2>&1; then
        pre-commit install
    else
        # uv-installed pre-commit lives inside .venv; invoke it via python -m.
        python -m pre_commit install
    fi
fi

echo ""
echo "=== Setup complete ==="
echo ""
echo "Next steps:"
echo "  ./scripts/check-all.sh    # Run every quality gate (should pass)"
echo "  ./scripts/test.sh         # Run unit tests"
