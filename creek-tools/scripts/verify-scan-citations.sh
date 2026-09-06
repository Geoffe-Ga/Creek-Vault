#!/usr/bin/env bash
# Verify every symbol a scan finding cites actually exists at the scan SHA.
#
# Issue #1651. The producer scans confabulated function names -- names that
# exist in NO revision of the file they cite, paraphrased from surrounding
# code. A prompt sentence asking the model to be careful does not fix that;
# only refusing the citation does.
#
# Reads newline-delimited JSON findings on stdin, one object per line, each
# carrying at least:
#     {"file": "creek-tools/creek/x.py", "symbol": "name", "lines": "10-20"}
# `symbol` may be absent (not every finding names one) or a JSON list.
#
# Exit 0 when every named symbol resolves at $SCAN_SHA; exit 1 naming each
# phantom, and 1 if a citation could not be checked at all -- an unreadable
# blob is exactly the condition a stale citation creates, so failing open
# there would make this gate decorative.
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SCAN_SHA="${SCAN_SHA:-$(git -C "$REPO_ROOT" rev-parse HEAD)}"
PYTHON="${PYTHON:-python3}"
RESOLVER="$REPO_ROOT/creek-tools/scripts/scan_citations.py"

if [ ! -f "$RESOLVER" ]; then
    echo "::error::resolver not found at $RESOLVER; refusing to report citations as verified"
    exit 1
fi

# Prove the interpreter runs BEFORE trusting any verdict it produces. $PYTHON
# also parses the findings JSON below, so an unusable one parses nothing,
# checks nothing, and exits 0 -- a gate reporting that it did nothing, which
# reads identically to a clean pass.
if ! "$PYTHON" -c "import ast, json" >/dev/null 2>&1; then
    echo "::error::interpreter '$PYTHON' is unusable; refusing to report citations as verified"
    exit 1
fi

failures=0
checked=0
lines_read=0

while IFS= read -r line; do
    [ -z "$line" ] && continue
    lines_read=$((lines_read + 1))
    while IFS=$'\t' read -r file symbol lines; do
        [ -z "${symbol:-}" ] && continue
        [ -z "${file:-}" ] && continue
        if [ "$file" = "MALFORMED" ]; then
            echo "::error::malformed findings JSON: $symbol"
            failures=$((failures + 1))
            continue
        fi
        checked=$((checked + 1))
        # Invoked by absolute path, never as `-m scripts.scan_citations`: the
        # documented usage is from the REPO ROOT, where `scripts` resolves to
        # the repo-root ./scripts/ (Ralph tooling) rather than to
        # creek-tools/scripts/, and creek-tools/pyproject.toml's package-find
        # never puts creek-tools/scripts on sys.path either. As a module this
        # reported every citation -- including real ones -- as a failure.
        # Pass --line when the finding carries one: the resolver then appends
        # "the lines cited hold X" to the PHANTOM message, which is the whole
        # remedy. Without it the common case gets the generic error.
        # ${arr[@]+"${arr[@]}"} rather than "${arr[@]}": macOS ships bash 3.2,
        # where an empty array expansion under `set -u` is an unbound-variable
        # error. The subprocess tests run with a restricted PATH whose bash is
        # that 3.2, which is how this was caught.
        line_args=()
        first="${lines%%-*}"
        if [ -n "${first:-}" ] && [ "$first" -eq "$first" ] 2>/dev/null; then
            line_args=(--line "$first")
        fi
        if ! out=$("$PYTHON" "$RESOLVER" \
                --repo "$REPO_ROOT" --sha "$SCAN_SHA" \
                --path "$file" --symbol "$symbol" ${line_args[@]+"${line_args[@]}"} 2>&1); then
            echo "::error::$out"
            failures=$((failures + 1))
        elif [ -n "${lines:-}" ]; then
            # The name exists somewhere in the file. Whether it is where the
            # finding says it is, is a weaker but still useful signal: a real
            # name 500 lines from the cited range sends a reader to the wrong
            # place. Warned, not failed -- a finding may legitimately cite a
            # helper called from those lines rather than defined in them.
            first="${lines%%-*}"
            if [ "$first" -eq "$first" ] 2>/dev/null; then
                at=$("$PYTHON" "$RESOLVER" --repo "$REPO_ROOT" \
                        --sha "$SCAN_SHA" --path "$file" --line "$first" 2>/dev/null || true)
                case "$at" in
                    *"$symbol"*) : ;;
                    ""|"<module level>") : ;;
                    *) echo "::warning::'$symbol' exists in $file but lines $lines hold '$at'; check the citation points where it claims" ;;
                esac
            fi
        fi
    done < <(printf '%s' "$line" | "$PYTHON" "$RESOLVER" --extract)
done

if [ "$failures" -gt 0 ]; then
    echo "::error::$failures phantom symbol citation(s) at ${SCAN_SHA:0:12}; refusing to file. Re-resolve each against the scan SHA and cite the real name."
    exit 1
fi

# Distrust a gate that reports it did nothing. Findings arrived but nothing was
# checked means the JSON never parsed, not that no finding named a symbol --
# and a silent 0 there is indistinguishable from a clean pass.
if [ "$lines_read" -gt 0 ] && [ "$checked" -eq 0 ]; then
    echo "::warning::verify-scan-citations: read $lines_read finding(s) but checked 0 symbols. If any finding names a function, class or method, it MUST declare a symbol field."
fi
echo "verify-scan-citations: $checked symbol citation(s) verified at ${SCAN_SHA:0:12}"
exit 0
