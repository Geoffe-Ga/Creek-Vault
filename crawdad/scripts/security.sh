#!/usr/bin/env bash
# scripts/security.sh — Bandit on the source tree, then pip-audit on
# both dependency surfaces (installed environment and exported uv.lock).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$(dirname "$SCRIPT_DIR")"

echo "=== bandit: source tree ==="
bandit -r crawdad -ll

# pip-audit runs twice because crawdad has two distinct dependency
# surfaces, and each one answers a different question.
#
# 1. The installed environment — "is what CI actually imports
#    vulnerable?" CI provisions with `pip install -e ".[dev]"`, and pip
#    honours neither uv.lock nor [tool.uv].constraint-dependencies:
#    both are invisible to it. Nothing else in the gate guards this
#    surface, so a floor can be correct in pyproject and in the lock
#    while CI still resolves a vulnerable transitive release.
# 2. The exported lock — "is the reproducibility contract `uv sync`
#    users install vulnerable?" All eight advisories of #979 lived
#    here: an environment-only audit reported "No known vulnerabilities
#    found" while uv.lock carried eight, so auditing the environment
#    alone would have missed this entire issue.
#
# The gap between those two answers is what let the drift go unnoticed.
# The sibling creek-tools gate gets away with a single bare pip-audit
# only because its CI installs FROM the lock (`uv pip install -r
# locked-requirements.txt`), which makes environment and lock the same
# artifact. crawdad's CI does not, so crawdad needs both.

echo "=== pip-audit: installed environment ==="
# No --strict here: the local `crawdad` package is not published to
# PyPI, so pip-audit always reports it as a benign SKIP. --strict would
# promote that permanent skip into a permanent false failure — do not
# "harden" this by adding it.
pip-audit

echo "=== pip-audit: exported uv.lock ==="
LOCK_REQUIREMENTS="$(mktemp)"
trap 'rm -f "$LOCK_REQUIREMENTS"' EXIT
# --locked doubles as a lock-freshness gate: the export fails instead
# of silently relocking when pyproject.toml and uv.lock have drifted.
# That failure is intentional — the fix is to run `uv lock`, never to
# drop --locked.
#
# The export deliberately keeps hashes (no --no-hashes): the hashed
# form lets `pip-audit --disable-pip` resolve the requirements without
# --no-deps and without emitting the "users are encouraged to fully
# hash" warning on every run.
#
# --quiet suppresses only the redundant copy of the export that `-o`
# otherwise also dumps to stdout (~145 KB of hashes per run, which
# would bury the audit results in the CI log). It does NOT silence
# failures: `--locked` still exits non-zero with "The lockfile at
# `uv.lock` needs to be updated" on stderr when the lock has drifted.
uv export --quiet --locked --all-extras --no-emit-project -o "$LOCK_REQUIREMENTS"
pip-audit --requirement "$LOCK_REQUIREMENTS" --disable-pip
