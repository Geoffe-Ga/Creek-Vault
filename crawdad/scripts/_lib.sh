#!/usr/bin/env bash
# scripts/_lib.sh - Shared helpers sourced by the crawdad task scripts.
#
# Not executable on its own; `source` it from another script after that
# script has set `set -euo pipefail`. Keep functions POSIX/bash-friendly
# and shellcheck-clean (this file is checked with `# shellcheck shell=bash`).
#
# The creek-tools twin of this file (creek-tools/scripts/_lib.sh) carries
# `creek_require_python_toolchain`, the same shape of guard for the same
# reason. This is a separate file rather than a shared one because the
# remediation differs: crawdad has no `dev-setup.sh`, so its message names
# `uv sync --all-extras` run from `crawdad/`.
#
# shellcheck shell=bash

# crawdad_require_python_module — guard a gate step against running with a
# tool that is missing from the *active interpreter*.
#
# Issue #1671. Every gate script here invokes its tool by bare name, so
# bash resolves it through PATH. When the tool is absent from the
# environment the gate is supposed to be checking, PATH does not fail — it
# keeps walking, and finds a copy somewhere else. Both of the failures that
# issue reported are that one behaviour seen from two sides:
#
#   * `pip-audit` was missing from `crawdad/.venv`, so a bare `pip-audit`
#     ran Homebrew's copy and audited `/opt/homebrew/opt/python@3.13`. It
#     reported two CVEs belonging to that interpreter and exited non-zero.
#     The gate looked like it was working. It was auditing a different
#     set of packages than the one it exists to gate.
#   * `vulture` was missing too, and the shared dead-code policy imports it
#     rather than shelling out, so that step died at `from vulture import
#     Vulture` with a traceback and no pointer to the fix.
#
# A silent wrong answer is the worse of those two, which is why this guard
# probes rather than trusting PATH. Probing the *module* — not `command -v`
# — is deliberate: the executable and the interpreter can disagree, and it
# is the interpreter's view that decides what actually gets audited.
#
# Callers pair this with a `python -m <module>` invocation. Together the two
# close the loop: the probe reports the problem in one actionable line, and
# `python -m` means the tool that then runs is the active interpreter's,
# never whatever PATH found first.
#
# This works on CI unchanged. The crawdad job provisions with `uv pip
# install --system` (#1501) and has no `.venv` at all, so a guard demanding
# a virtualenv path would fail there; a guard asking the running
# interpreter whether it can import the module is true in both places.
#
# Args:
#   $1 — the Python module to probe for, e.g. "pip_audit".
#   $2 (optional) — display name for the message; defaults to $1.
crawdad_require_python_module() {
    local module="$1"
    local display="${2:-$1}"

    if python -c "import ${module}" >/dev/null 2>&1; then
        return 0
    fi

    echo "error: ${display} is not installed in the active interpreter" >&2
    echo "       (could not import ${module})." >&2
    echo "       Run 'uv sync --all-extras' from crawdad/ and activate" >&2
    echo "       .venv, or the gate would check whatever copy PATH finds" >&2
    echo "       instead of this project (#1671)." >&2
    return 1
}
