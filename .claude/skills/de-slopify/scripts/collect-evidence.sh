#!/usr/bin/env bash
# collect-evidence.sh — read-only slop-evidence collector for the de-slopify skill.
#
# Runs the repo's existing static-analysis toolbox plus grep heuristics and
# writes every raw result into an output directory. It NEVER modifies tracked
# files and NEVER fails the run because a single tool is missing or unhappy —
# each tool's exit status is captured, not propagated, so the agent always gets
# a complete evidence bundle to corroborate against.
#
# Usage:
#   scripts/collect-evidence.sh [OUTPUT_DIR]
#
# OUTPUT_DIR defaults to "$SCRATCHPAD/deslop-evidence" if SCRATCHPAD is set,
# else a mktemp dir. The chosen directory is printed on the last line so a
# caller can capture it:  EVID=$(scripts/collect-evidence.sh | tail -1)
#
# Exit codes: 0 always (collection is best-effort). 2 only on a setup error
# (no git repo / cannot create output dir).

set -uo pipefail

# --- locate the repo root (this script lives in .claude/skills/de-slopify/scripts) ---
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../../.." && pwd)"
if [[ ! -d "$REPO_ROOT/.git" ]]; then
  echo "collect-evidence: not a git repo at $REPO_ROOT" >&2
  exit 2
fi
cd "$REPO_ROOT" || exit 2

# --- output dir ---
OUT="${1:-${SCRATCHPAD:+$SCRATCHPAD/deslop-evidence}}"
if [[ -z "$OUT" ]]; then
  OUT="$(mktemp -d)"
fi
if ! mkdir -p "$OUT"; then
  echo "collect-evidence: cannot create output dir $OUT" >&2
  exit 2
fi

# Python source roots in this monorepo (only the ones that exist are scanned).
# creek-tools is a flat-layout package (creek/, creek_mcp/), and crawdad is the
# Discord-bot subproject (crawdad/crawdad/). There is no frontend.
PY_DIRS=()
for d in creek-tools/creek creek-tools/creek_mcp crawdad/crawdad; do
  [[ -d "$d" ]] && PY_DIRS+=("$d")
done
# Shared tool config + dependency manifest live under creek-tools.
PY_CONFIG="creek-tools/pyproject.toml"
PY_REQS="creek-tools/requirements.txt"

log() { echo ">>> $*" >&2; }

# Activate the project venv if present (so backend tools resolve).
if [[ -f .venv/bin/activate ]]; then
  # shellcheck disable=SC1091
  source .venv/bin/activate
fi

# run <outfile> <cmd...> — run a tool, capture stdout+stderr and exit code,
# never abort the script. Skips gracefully if the binary is absent.
run() {
  local out="$1"; shift
  local bin="$1"
  if ! command -v "$bin" >/dev/null 2>&1; then
    echo "SKIPPED: $bin not installed" >"$OUT/$out"
    log "skip $bin (not installed)"
    return 0
  fi
  log "run $*"
  if "$@" >"$OUT/$out" 2>&1; then
    echo "[exit 0]" >>"$OUT/$out"
  else
    echo "[exit $?]" >>"$OUT/$out"
  fi
  return 0
}

# ----------------------------------------------------------------------------
# Python — read-only analysis (creek-tools + crawdad)
# ----------------------------------------------------------------------------
if [[ ${#PY_DIRS[@]} -gt 0 ]]; then
  run ruff.json            ruff check "${PY_DIRS[@]}" --config="$PY_CONFIG" --output-format=json
  run vulture.txt          vulture "${PY_DIRS[@]}" --min-confidence 80
  run radon-cc.txt         radon cc "${PY_DIRS[@]}" -s -n C
  run radon-mi.txt         radon mi "${PY_DIRS[@]}" -s
  run mypy.txt             mypy "${PY_DIRS[@]}" --config-file="$PY_CONFIG"
  run bandit.json          bandit -r "${PY_DIRS[@]}" -f json -c "$PY_CONFIG"
  run interrogate.txt      interrogate "${PY_DIRS[@]}" -v
  run pip-audit.txt        pip-audit -r "$PY_REQS"
  run detect-secrets.txt   detect-secrets scan "${PY_DIRS[@]}"
fi

# ----------------------------------------------------------------------------
# Cross-cutting grep heuristics (candidates only — need a 2nd signal)
# ----------------------------------------------------------------------------
GREP_BIN="grep"
GREP_FLAGS=(-rnE)
if command -v rg >/dev/null 2>&1; then
  GREP_BIN="rg"
  GREP_FLAGS=(-n)
fi
SEARCH_PATHS=("${PY_DIRS[@]}")

greps() {
  local out="$1" pat="$2"
  if [[ ${#SEARCH_PATHS[@]} -eq 0 ]]; then return 0; fi
  "$GREP_BIN" "${GREP_FLAGS[@]}" "$pat" "${SEARCH_PATHS[@]}" >"$OUT/$out" 2>/dev/null \
    || echo "(no matches)" >"$OUT/$out"
}

greps grep-stubs.txt        'NotImplementedError|not implemented|throw new Error\(.?not implemented|return None\s*#\s*TODO|\bpass\s*#\s*(stub|placeholder)'
greps grep-ai-tells.txt     'In a real implementation|real implementation|placeholder|for now|as an AI|should probably'
greps grep-debt.txt         'TODO|FIXME|HACK|XXX'
greps grep-escape-hatch.txt 'type: ?ignore|@ts-ignore|@ts-nocheck|eslint-disable|# ?noqa|cast\(Any'
greps grep-swallow.txt      'except (Exception|BaseException)?\s*:|catch\s*\([^)]*\)\s*\{\s*\}|\.catch\(\(\)\s*=>\s*\{?\s*\}?\)'
greps grep-commented.txt    '^\s*#\s*(def |class |return |if |for |while |import |from )'
greps grep-any.txt          ':\s*any\b|<any>|as any'

# Git churn / hotspots (top 30 most-changed files in the last 90 days).
if command -v git >/dev/null 2>&1; then
  git log --since="90 days ago" --format= --name-only 2>/dev/null \
    | grep -E '^(creek-tools|crawdad)/' \
    | sort | uniq -c | sort -rn | head -30 >"$OUT/churn.txt" 2>/dev/null \
    || echo "(churn unavailable)" >"$OUT/churn.txt"
fi

# Reading targets: the largest source files by line count. These — together
# with churn.txt — are where the reading pass should start, because size and
# change-frequency are where bloaters, duplication, and god-objects accumulate.
{
  echo "# Largest source files (LoC) — prime reading-pass targets"
  if [[ ${#SEARCH_PATHS[@]} -gt 0 ]]; then
    find "${SEARCH_PATHS[@]}" -type f \
      -name '*.py' \
      -not -path '*/node_modules/*' -print0 2>/dev/null \
      | xargs -0 wc -l 2>/dev/null | sort -rn | sed '/ total$/d' | head -30
  fi
} >"$OUT/reading-targets.txt"

# ----------------------------------------------------------------------------
# Manifest
# ----------------------------------------------------------------------------
{
  echo "# De-Slop Evidence Bundle"
  echo "Repo:    $REPO_ROOT"
  echo "Out:     $OUT"
  echo
  echo "## Files"
  ls -1 "$OUT" | sed 's/^/  - /'
  echo
  echo "Each *.json / *.txt holds raw tool or grep output. Every entry is a"
  echo "CANDIDATE only — apply the Two-Signal Rule from detection-playbook.md"
  echo "before filing anything. Tool exit codes are appended as [exit N]."
  echo
  echo "## IMPORTANT — this bundle is a MAP, not the findings"
  echo "The linter outputs (ruff/mypy/radon/bandit/eslint/tsc) are TABLE STAKES:"
  echo "the repo already passes them in pre-commit and CI, so they cannot be"
  echo "findings. Do NOT file complexity grades, lint rules, or type errors."
  echo "Use churn.txt + reading-targets.txt to drive a Task fan-out that READS"
  echo "the source for what linters cannot see (dead/stubbed/orphaned code,"
  echo "duplication, architecture, lying flags, verbosity, comment slop, AI"
  echo "tells, weak tests). That reading pass is the actual audit."
} >"$OUT/README.txt"

log "evidence collected in $OUT"
echo "$OUT"
