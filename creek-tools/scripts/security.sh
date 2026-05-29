#!/usr/bin/env bash
# scripts/security.sh - Run security checks with Bandit and pip-audit
# Usage: ./scripts/security.sh [--full] [--verbose] [--help]

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

FULL=false
VERBOSE=false

# Parse command line arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        --full)
            FULL=true
            shift
            ;;
        --verbose)
            VERBOSE=true
            shift
            ;;
        --help)
            cat << EOF
Usage: $(basename "$0") [OPTIONS]

Run security checks using Bandit and pip-audit.

OPTIONS:
    --full      Run comprehensive security scan
    --verbose   Show detailed output
    --help      Display this help message

EXIT CODES:
    0           No security issues found
    1           Security issues found
    2           Error running checks

EXAMPLES:
    $(basename "$0")             # Run basic security checks
    $(basename "$0") --full      # Run comprehensive scan
    $(basename "$0") --verbose   # Show detailed output
EOF
            exit 0
            ;;
        *)
            echo "Error: Unknown option: $1" >&2
            exit 2
            ;;
    esac
done

cd "$PROJECT_ROOT"

# Set verbosity
if $VERBOSE; then
    set -x
fi

echo "=== Security Checks (Bandit) ==="

# Run Bandit.
#
# Severity threshold ``-ll`` (medium-or-above) matches both
# .github/workflows/ci.yml and the CLAUDE.md §6.1 policy
# ("Bandit: zero medium-or-above findings"). Aligning local with CI
# closes the GAP-007 parity gap — a clean ``check-all.sh`` pass means
# CI agrees on the same severity scope.
if $VERBOSE; then
    echo "Running Bandit security scanner (medium-or-above)..."
fi
bandit -r creek/ -ll || { echo "✗ Bandit found issues" >&2; exit 1; }

echo "=== Dependency Vulnerability Check (pip-audit) ==="

# Run pip-audit
if $VERBOSE; then
    echo "Running pip-audit dependency checker..."
fi
# Documented CVE in build/install tooling. Excluded with a
# justification + advisory link, kept in sync with
# .github/workflows/ci.yml. Audit this list at every release.
#
# Re-audit 2026-05-23 (issue #249): all 23 advisories previously
# suppressed for torch, transformers, joblib, pyjwt, pip, and wheel
# are no longer flagged by pip-audit's PyPI vulnerability service
# against the currently-locked versions (torch 2.12.0, transformers
# 5.8.1, joblib 1.5.3, pyjwt 2.12.1, pip 26.1.1, wheel 0.47.0). The
# PyPI advisory feed returns zero vulnerabilities for each of those
# pinned versions, so the suppressions were dropped per the
# cve-remediation "no suppression without an active finding" rule.
#
#   - PYSEC-2022-42969: ReDoS in py.path.svnwc. ``py`` is a transitive
#     dependency pulled in by ``interrogate 1.7.0`` (also the latest
#     release); ``py`` itself is abandoned at 1.11.0 with no
#     forthcoming fix. creek-tools never invokes
#     ``py.path.svnwc``-backed code paths (SVN handling is unused), so
#     this advisory is not reachable.
#     https://github.com/advisories/GHSA-w596-4wvx-j9j6
#     Re-audit by 2026-11-23, or sooner if ``interrogate`` drops its
#     ``py`` dependency.
pip-audit \
    --ignore-vuln PYSEC-2022-42969 \
    || { echo "✗ pip-audit found vulnerable dependencies" >&2; exit 1; }

if $FULL; then
    echo "=== Comprehensive Security Scan ==="

    # Check for hardcoded secrets
    if command -v detect-secrets &> /dev/null; then
        if $VERBOSE; then
            echo "Running detect-secrets scan..."
        fi
        detect-secrets scan . || true
    fi
fi

echo "✓ Security checks passed"
exit 0
