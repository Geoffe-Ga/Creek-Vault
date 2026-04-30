#!/usr/bin/env bash
# scripts/coverage-per-file.sh - Per-file coverage threshold gate
#
# Reads reports/coverage.json (or coverage.json) produced by a prior
# coverage run and fails if any source file falls below the per-file
# threshold. The aggregate threshold is enforced separately via
# pytest --cov-fail-under in coverage.sh.
#
# An allowlist of waivers lives in scripts/coverage-waivers.txt. Each
# line is "<path> <reason>" (single space). Files on the allowlist are
# subject to the WAIVER_FLOOR (a non-zero baseline that prevents
# regression) instead of the strict THRESHOLD.
#
# Usage: ./scripts/coverage-per-file.sh [--threshold N]
#                                       [--waiver-floor N]
#                                       [--report PATH]

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

THRESHOLD=80
WAIVER_FLOOR=65
REPORT=""
WAIVERS_FILE="$SCRIPT_DIR/coverage-waivers.txt"

while [[ $# -gt 0 ]]; do
    case $1 in
        --threshold)
            THRESHOLD="$2"
            shift 2
            ;;
        --waiver-floor)
            WAIVER_FLOOR="$2"
            shift 2
            ;;
        --report)
            REPORT="$2"
            shift 2
            ;;
        --help)
            cat << EOF
Usage: $(basename "$0") [--threshold N] [--waiver-floor N] [--report PATH]

Fail if any source file's line coverage is below the threshold. (The
aggregate gate enforces *branch* coverage at 90% — see ADR-0001 for
the line-vs-branch rationale at the per-file level.)

Files on the waiver allowlist (scripts/coverage-waivers.txt) are
subject to the lower waiver-floor instead, but still fail if they
regress below it.

Defaults: threshold=80%, waiver-floor=65%, report=reports/coverage.json
(falls back to coverage.json in the project root).

OPTIONS:
    --threshold N      Strict per-file minimum (default 80)
    --waiver-floor N   Floor for waivered files (default 65)
    --report PATH      Path to coverage.json (default: auto-detect)
    --help             Display this help message
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

if [[ -z "$REPORT" ]]; then
    if [[ -f reports/coverage.json ]]; then
        REPORT="reports/coverage.json"
    elif [[ -f coverage.json ]]; then
        REPORT="coverage.json"
    else
        echo "✗ coverage.json not found; run coverage.sh first" >&2
        exit 2
    fi
fi

THRESHOLD="$THRESHOLD" WAIVER_FLOOR="$WAIVER_FLOOR" \
REPORT="$REPORT" WAIVERS_FILE="$WAIVERS_FILE" \
python <<'PYEOF'
import json
import os
import sys
from pathlib import Path

threshold = float(os.environ["THRESHOLD"])
waiver_floor = float(os.environ["WAIVER_FLOOR"])
report = Path(os.environ["REPORT"])
waivers_path = Path(os.environ["WAIVERS_FILE"])

waivers: dict[str, str] = {}
if waivers_path.exists():
    for line in waivers_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        path, _, reason = line.partition(" ")
        waivers[path] = reason or "(no reason recorded)"

data = json.loads(report.read_text())
files = data.get("files", {})
strict_failures: list[tuple[str, float]] = []
waiver_failures: list[tuple[str, float, str]] = []

# Detect orphaned waivers (entries that name a file no longer in the
# coverage report — e.g. after a rename or deletion). An orphan is a
# silent loophole: if the waivered file was renamed but kept its low
# coverage, the new name would NOT be on the waiver list and would fail
# the strict gate, which is correct behaviour. But if a file was
# deleted, the waiver should be removed too.
orphan_waivers = sorted(set(waivers) - set(files))

# Count strictly: only files actually in the coverage report.
files_in_report = set(files)
applied_waivers = sorted(set(waivers) & files_in_report)

for name, summary in files.items():
    pct = summary["summary"]["percent_covered"]
    if name in waivers:
        if pct < waiver_floor:
            waiver_failures.append((name, pct, waivers[name]))
    elif pct < threshold:
        strict_failures.append((name, pct))

ok = True

if strict_failures:
    ok = False
    print(f"Per-file coverage threshold {threshold:.1f}% violated:")
    for name, pct in sorted(strict_failures, key=lambda x: x[1]):
        print(f"  FAIL {pct:6.2f}% < {threshold:.1f}%  {name}")

if waiver_failures:
    ok = False
    print(f"Waiver floor {waiver_floor:.1f}% violated (regression):")
    for name, pct, reason in sorted(waiver_failures, key=lambda x: x[1]):
        print(f"  FAIL {pct:6.2f}% < {waiver_floor:.1f}%  {name}  ({reason})")

if orphan_waivers:
    ok = False
    print("Orphaned waivers (file not in coverage report):")
    for name in orphan_waivers:
        print(f"  ORPHAN  {name}  — remove from coverage-waivers.txt")

if not ok:
    sys.exit(1)

# Counts derive from set membership, not arithmetic on len(), so a
# stale waiver entry can never produce a negative "strict" count.
strict_count = len(files_in_report) - len(applied_waivers)
print(
    f"✓ {strict_count} files >= {threshold:.1f}%, "
    f"{len(applied_waivers)} waivered files >= {waiver_floor:.1f}%"
)
PYEOF
