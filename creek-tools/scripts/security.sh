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

# Run Bandit
if $VERBOSE; then
    echo "Running Bandit security scanner..."
fi
bandit -r creek/ || { echo "✗ Bandit found issues" >&2; exit 1; }

echo "=== Dependency Vulnerability Check (pip-audit) ==="

# Run pip-audit
if $VERBOSE; then
    echo "Running pip-audit dependency checker..."
fi
# Documented CVEs in build/install tooling. Each is excluded with a
# justification + advisory link, kept in sync with
# .github/workflows/ci.yml. Audit this list at every release.
#
#   - PYSEC-2022-42969: ReDoS in py.path.svnwc; py is a transitive dev
#     dep (pulled in by pytest plugins) and the affected code path is
#     unused. https://github.com/advisories/GHSA-w596-4wvx-j9j6
#   - CVE-2025-8869: pip — symlink TOCTOU during sdist install; the
#     installer never runs against untrusted sdists in CI.
#     https://nvd.nist.gov/vuln/detail/CVE-2025-8869
#   - CVE-2026-1703: pip — index-URL parsing; CI installs from declared
#     requirements only, so untrusted indexes are not in scope.
#     https://nvd.nist.gov/vuln/detail/CVE-2026-1703
#   - CVE-2026-3219: pip — no fix published yet; same scope as above.
#     https://nvd.nist.gov/vuln/detail/CVE-2026-3219
#   - CVE-2026-24049: wheel — RECORD parsing edge case; wheel is build-
#     tooling only and never reaches the production runtime.
#     https://nvd.nist.gov/vuln/detail/CVE-2026-24049
#
# CVE-2026-6357 (pip < 26.1) is fixed upstream; ``requirements-dev.txt``
# pins ``pip>=26.1`` so pip-audit no longer reports it. No --ignore-vuln
# entry needed here.
#
# torch / transformers — transitive via the ``embeddings`` extra
# (sentence-transformers). No patched release exists: verified
# 2026-05-21 that torch 2.12.0 and transformers 5.9.0 are the latest
# published versions and pip-audit still reports every advisory below.
#   - torch PYSEC-2025-189..197, PYSEC-2025-210, PYSEC-2026-139:
#     memory-corruption / DoS in low-level ops (jit.script, lstm_cell,
#     rnn utilities, CUDA allocator, ...); all require local access and
#     attacker-crafted inputs. Several advisories note upstream has not
#     yet shipped a fix.
#   - transformers PYSEC-2025-211..218: RCE via converting or loading a
#     malicious checkpoint / model file.
# creek-tools touches this stack only through the FEAT-018 compost
# gate, which embeds the user's own vault fragments with a pinned,
# known sentence-transformers model — it never converts checkpoints nor
# loads untrusted model files, so none of these paths are reachable.
# Re-audit by 2026-06-20 and at every release; drop each entry the
# moment an upstream fix ships.
#
# joblib PYSEC-2024-277 and pyjwt PYSEC-2025-183 — both transitive,
# both already at their latest release (joblib 1.5.3, pyjwt 2.12.1),
# and both formally disputed by their suppliers: the joblib path is
# reachable only when caching trusted content, and the pyjwt key
# length is the calling application's choice. creek-tools controls
# neither path. Re-audit by 2026-06-20.
pip-audit \
    --ignore-vuln PYSEC-2022-42969 \
    --ignore-vuln CVE-2025-8869 \
    --ignore-vuln CVE-2026-1703 \
    --ignore-vuln CVE-2026-3219 \
    --ignore-vuln CVE-2026-24049 \
    --ignore-vuln PYSEC-2025-189 \
    --ignore-vuln PYSEC-2025-190 \
    --ignore-vuln PYSEC-2025-191 \
    --ignore-vuln PYSEC-2025-192 \
    --ignore-vuln PYSEC-2025-193 \
    --ignore-vuln PYSEC-2025-194 \
    --ignore-vuln PYSEC-2025-195 \
    --ignore-vuln PYSEC-2025-196 \
    --ignore-vuln PYSEC-2025-197 \
    --ignore-vuln PYSEC-2025-210 \
    --ignore-vuln PYSEC-2026-139 \
    --ignore-vuln PYSEC-2025-211 \
    --ignore-vuln PYSEC-2025-212 \
    --ignore-vuln PYSEC-2025-213 \
    --ignore-vuln PYSEC-2025-214 \
    --ignore-vuln PYSEC-2025-215 \
    --ignore-vuln PYSEC-2025-216 \
    --ignore-vuln PYSEC-2025-217 \
    --ignore-vuln PYSEC-2025-218 \
    --ignore-vuln PYSEC-2024-277 \
    --ignore-vuln PYSEC-2025-183 \
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
