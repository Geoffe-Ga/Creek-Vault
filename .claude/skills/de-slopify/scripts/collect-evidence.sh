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

# Activate the project venv if present (so the Python tools resolve).
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
  # 60, not 80. Vulture scores an unused function/method/class/property/
  # attribute at 60%, so a floor of 80 excludes the entire dead-symbol
  # tier — the defect fixed in issue #1395. This is evidence collection,
  # not a gate, so the extra noise is the right trade: a human or agent
  # triages the output, and a missing finding cannot be triaged at all.
  # The actual gate lives in creek-tools/scripts/lint-vulture.sh, which
  # applies per-type floors; its verdict will not match this raw sweep.
  run vulture.txt          vulture "${PY_DIRS[@]}" --min-confidence 60
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

greps grep-stubs.txt        'NotImplementedError|not implemented|return None\s*#\s*TODO|\bpass\s*#\s*(stub|placeholder)'
greps grep-ai-tells.txt     'In a real implementation|real implementation|placeholder|for now|as an AI|should probably'
greps grep-debt.txt         'TODO|FIXME|HACK|XXX'
greps grep-escape-hatch.txt 'type: ?ignore|# ?noqa|cast\(Any'
greps grep-swallow.txt      'except (Exception|BaseException)?\s*:'
greps grep-commented.txt    '^\s*#\s*(def |class |return |if |for |while |import |from )'
greps grep-any.txt          ':\s*Any\b|->\s*Any\b|dict\[str,\s*Any\]'

# Git churn / hotspots (top 30 most-changed files in the last 90 days).
# PRIORITIZATION SIGNAL ONLY — churn (and reading-targets below) decide which
# area the reading pass starts with; they NEVER decide which areas are skipped.
# Files untouched in 90 days never appear here, so a run anchored to this list
# would never read stable code. Coverage is governed by area-inventory.txt
# (every area must be read each run); this is just the order to read it in.
if command -v git >/dev/null 2>&1; then
  git log --since="90 days ago" --format= --name-only 2>/dev/null \
    | grep -E '^(creek-tools|crawdad)/' \
    | sort | uniq -c | sort -rn | head -30 >"$OUT/churn.txt" 2>/dev/null \
    || echo "(churn unavailable)" >"$OUT/churn.txt"
fi

# Reading targets: the largest source files by line count. PRIORITIZATION ONLY
# (same caveat as churn.txt) — together with churn they say where to START
# reading, because size and change-frequency are where bloaters, duplication,
# and god-objects accumulate. They are NOT the coverage set.
{
  echo "# Largest source files (LoC) — prime reading-pass START targets."
  echo "# Prioritization order only; NOT a coverage filter (see area-inventory.txt)."
  if [[ ${#SEARCH_PATHS[@]} -gt 0 ]]; then
    find "${SEARCH_PATHS[@]}" -type f \
      -name '*.py' \
      -not -path '*/__pycache__/*' -print0 2>/dev/null \
      | xargs -0 wc -l 2>/dev/null | sort -rn | sed '/ total$/d' | head -30
  fi
} >"$OUT/reading-targets.txt"

# ----------------------------------------------------------------------------
# Area inventory — the AUTHORITATIVE coverage set for the reading pass.
# EVERY area listed here MUST be read every run (whole-codebase audit). Churn /
# reading-targets decide the ORDER only. The coverage ledger must enumerate
# every area below and mark it read this run; a "0 findings" verdict is only
# defensible when the ledger covers this entire inventory — never "delta since
# last run". Best-effort + never-fail: missing dirs are simply skipped.
# ----------------------------------------------------------------------------
{
  echo "# Area inventory — the coverage set the reading pass MUST cover in full."
  echo "# Every area must be read each run; churn/reading-targets are order only."
  echo
  echo "## creek pipeline stages (creek-tools/creek/*)"
  [[ -d creek-tools/creek ]] \
    && find creek-tools/creek -mindepth 1 -maxdepth 1 -type d ! -name '__pycache__' 2>/dev/null | sort
  echo
  echo "## creek top-level modules"
  [[ -d creek-tools/creek ]] \
    && find creek-tools/creek -maxdepth 1 -name '*.py' ! -name '__init__.py' 2>/dev/null | sort
  echo
  echo "## MCP server surface"
  [[ -d creek-tools/creek_mcp ]] && echo "creek-tools/creek_mcp"
  echo
  echo "## crawdad Discord bot (incl. llm/ and builtin_workflows/)"
  [[ -d crawdad/crawdad ]] \
    && find crawdad/crawdad -mindepth 1 -maxdepth 1 \
         \( -type d ! -name '__pycache__' -o -name '*.py' ! -name '__init__.py' \) \
         2>/dev/null | sort
  echo
  echo "## shell tooling"
  for d in creek-tools/scripts crawdad/scripts scripts; do
    [[ -d "$d" ]] && echo "$d"
  done
} >"$OUT/area-inventory.txt"

# ----------------------------------------------------------------------------
# Manifest
# ----------------------------------------------------------------------------
{
  echo "# De-Slop Evidence Bundle"
  echo "Repo:    $REPO_ROOT"
  echo "Out:     $OUT"
  echo
  echo "## Files"
  for f in "$OUT"/*; do
    [[ -e "$f" ]] && echo "  - $(basename "$f")"
  done
  echo
  echo "Each *.json / *.txt holds raw tool or grep output. Every entry is a"
  echo "CANDIDATE only — apply the Two-Signal Rule from detection-playbook.md"
  echo "before filing anything. Tool exit codes are appended as [exit N]."
  echo
  echo "## IMPORTANT — this bundle is a MAP, not the findings"
  echo "The linter outputs (ruff/mypy/radon/bandit/interrogate) are TABLE STAKES:"
  echo "the repo already passes them in pre-commit and CI, so they cannot be"
  echo "findings. Do NOT file complexity grades, lint rules, or type errors."
  echo "Drive a Task fan-out that READS the source for what linters cannot see"
  echo "(dead/stubbed/orphaned code, duplication, architecture, lying flags,"
  echo "verbosity, comment slop, AI tells, weak tests). That reading pass is the"
  echo "actual audit."
  echo
  echo "## COVERAGE IS MANDATORY AND WHOLE-CODEBASE"
  echo "area-inventory.txt is the AUTHORITATIVE coverage set: the reading pass"
  echo "MUST cover EVERY area in it EVERY run. churn.txt + reading-targets.txt"
  echo "are PRIORITIZATION ORDER ONLY — they say where to start, never which"
  echo "areas to skip. A clean linter bundle or an unchanged file is NOT a reason"
  echo "to skip reading an area. 'Delta-focused' / 'since last run' / 'building on"
  echo "last week's baseline' scoping is FORBIDDEN. A '0 findings' verdict is only"
  echo "valid when the coverage ledger enumerates this entire inventory as read"
  echo "this run."
} >"$OUT/README.txt"

log "evidence collected in $OUT"
echo "$OUT"
