#!/usr/bin/env bash
# scripts/security.sh — Bandit on the source tree, then pip-audit on
# both dependency surfaces (installed environment and exported uv.lock).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$(dirname "$SCRIPT_DIR")"

# shellcheck source=scripts/_lib.sh
source "$SCRIPT_DIR/_lib.sh"

# Fail with an actionable message rather than auditing whatever copy of
# pip-audit PATH finds first (#1671). Both invocations below run through
# `python -m`, so this probe and they agree by construction on which
# environment is under audit.
crawdad_require_python_module pip_audit pip-audit || exit 2

echo "=== bandit: source tree ==="
bandit -r crawdad -ll

# pip-audit runs twice because crawdad has two distinct dependency
# surfaces, and each one answers a different question.
#
# 1. The installed environment — "is what is actually importable
#    vulnerable?" Since #1501, CI provisions this environment FROM the
#    exported lock (`uv export --locked --all-extras --no-emit-project
#    --no-hashes`, piped into `uv pip install --system -r ...`, then
#    `uv pip install --system --no-deps -e .`), so on a healthy tree
#    this pass and the lock pass below largely agree. They are not the
#    same artifact, though: the editable install layers the local
#    `crawdad` package on top, and whatever the interpreter's own
#    ensurepip seeded, or a PEP 517 build backend left behind, is
#    present in the environment and invisible to a plain lock export.
#    This pass also stands as the regression detector for #1501 itself:
#    pip honours neither uv.lock nor [tool.uv].constraint-dependencies,
#    so if a future edit reintroduces a live resolve (reverting to
#    `pip install -e ".[dev]"`, say), the lock pass below would stay
#    clean while this pass alone would report the drift.
# 2. The exported lock — "is the reproducibility contract `uv sync`
#    users install vulnerable?" All eight advisories of #979 lived
#    here: an environment-only audit reported "No known vulnerabilities
#    found" while uv.lock carried eight, so auditing the environment
#    alone would have missed this entire issue.
#
# The gap between those two answers is what let the drift of #979 go
# unnoticed, and #1501 narrowed that gap without closing it: the
# installed environment is now DERIVED from the lock rather than
# resolved independently of it, but a derived artifact is still not a
# byte-identical copy of the lock export, so both passes keep running.

echo "=== pip-audit: installed environment ==="
# No --strict here: the local `crawdad` package is not published to
# PyPI, so pip-audit always reports it as a benign SKIP. --strict would
# promote that permanent skip into a permanent false failure — do not
# "harden" this by adding it.
#
# `python -m pip_audit`, not a bare `pip-audit`: the bare form is resolved
# by PATH, and when this project's environment has no pip-audit of its own
# PATH silently supplies another one, which then audits the interpreter it
# belongs to instead of this one (#1671). Still bare in the sense that
# matters — no -r, so it inspects what is actually installed.
python -m pip_audit

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
python -m pip_audit --requirement "$LOCK_REQUIREMENTS" --disable-pip
