#!/usr/bin/env bash
# scripts/_lib.sh - Shared helpers sourced by the creek-tools task scripts.
#
# Not executable on its own; `source` it from another script after that
# script has set `set -euo pipefail`. Keep functions POSIX/bash-friendly
# and shellcheck-clean (this file is checked with `# shellcheck shell=bash`).
#
# shellcheck shell=bash

# creek_require_python_toolchain — guard against running the Python
# toolchain (pytest/mypy/coverage/etc.) on a checkout that never had the
# dev dependencies installed.
#
# On a fresh clone with only the system interpreter, a bare
# `python -m pytest` fails with an opaque `No module named pytest` and no
# pointer to the fix. This guard runs an explicit import probe and, on
# failure, prints a single actionable line to stderr and returns non-zero
# so the caller can exit before emitting the confusing native error.
#
# When the environment is set up correctly (`.venv` activated or deps
# installed into the active interpreter) the probe succeeds and this
# function is a no-op, so green runs stay green.
#
# Args:
#   $1 (optional) — the Python module to probe for; defaults to "pytest".
creek_require_python_toolchain() {
    local module="${1:-pytest}"

    if python -c "import ${module}" >/dev/null 2>&1; then
        return 0
    fi

    echo "error: Python toolchain not found (could not import ${module})." >&2
    echo "       Run ./scripts/dev-setup.sh (or 'uv sync --all-extras' and" >&2
    echo "       activate .venv) first." >&2
    return 1
}
