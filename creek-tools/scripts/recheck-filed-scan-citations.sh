#!/usr/bin/env bash
# Re-verify the citations a scan run actually filed, without asking the agent.
#
# Issue #1700. #1651 made every scan finding declare its `symbol` and added
# `verify-scan-citations.sh`, which resolves each name against the scan SHA's
# blob. But that gate ran ONLY because `_claude-scan.yml`'s `prompt:` asked the
# agent to run it -- and the agent holds `Bash`, `issues: write` and a
# `--max-turns 40` budget it can exhaust AFTER `gh issue create` and BEFORE the
# verification. So a phantom citation was blocked when the agent complied, not
# made unrepresentable. This script is the backstop: it reads what was filed.
#
# It asks the model for nothing. It lists the label's issues, subtracts the
# snapshot taken before the agent ran, parses each surviving body's own
# `File(s):` / `Symbol(s):` citations and its own recorded scan SHA, and pipes
# them through `verify-scan-citations.sh` VERBATIM -- one implementation of
# symbol resolution, not a second copy.
#
# ---------------------------------------------------------------------------
# WHY A SNAPSHOT, AND NOT `--search "created:>=<run start>"`
# ---------------------------------------------------------------------------
# `gh issue list --search` goes through GitHub's SEARCH INDEX, which lags issue
# creation by seconds to minutes. An issue filed moments ago by the very run
# being checked is exactly the document most likely to be missing from it. The
# step would then enumerate ZERO issues, verify nothing, and exit 0 -- a gate
# reporting that it did nothing, which reads identically to a clean pass. That
# is the same failure class this script exists to close, so the selector is a
# set difference over the non-indexed REST issues listing, and `RUN_STARTED_AT`
# is only ever a secondary filter.
#
# ---------------------------------------------------------------------------
# EVERY UNCERTAINTY FAILS CLOSED
# ---------------------------------------------------------------------------
#   * a missing or failed pre-agent snapshot  -> exit 1, never "skip the check"
#   * a non-zero `gh`                         -> exit 1
#   * a listing whose size equals the limit   -> exit 1 (it may be truncated,
#     and a truncated listing makes an OLD issue look new, which would post a
#     correction comment on somebody else's issue)
#   * new issues but not one parseable citation -> exit 1
# A failed correction comment is the one exception: it is a `::warning::`, and
# the phantom still fails the job.
#
# TWO THINGS THIS DELIBERATELY DOES NOT CLOSE, stated rather than discovered:
#   * An issue a human files under the same `scan:<name>` label WHILE the scan
#     is running falls inside the set difference and is treated as this run's.
#     `concurrency:` only stops two scans of the same name racing each other,
#     not a manual filing. It fails toward "correct verdict, wrong run
#     attributed" — never toward a silent pass — so it is a misattribution,
#     not a hole.
#   * An issue that ALREADY existed and that the agent EDITED during its
#     dedupe pass is outside the set difference by construction. Widening to
#     "every issue carrying the label" would re-verify the whole history on
#     every run and comment on issues this run never touched.
#
# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------
#   SCAN_LABEL           `scan:<name>`; charset-allowlisted before it reaches
#                        `gh`, mirroring `_claude-scan.yml`'s scan_name guard.
#   ISSUES_BEFORE        Comma-separated issue numbers that existed before the
#                        agent ran. Empty is legal (a first run) but the
#                        snapshot must still have SUCCEEDED.
#   ISSUES_SNAPSHOT_OK   Must be exactly `true`.
#   RUN_STARTED_AT       ISO-8601 instant, secondary filter only.
#   SCAN_SHA             Fallback revision for a body that records none.
#   GITHUB_REPOSITORY    owner/repo; passed as `--repo` when non-empty.
#   GH_TOKEN             Read by `gh`.
#   GH                   `gh` override, for the subprocess tests. Default `gh`.
#   PYTHON               Interpreter override. Default `python3`.
#
# Exit codes:
#   0 - every citation this run filed resolves at its own scan SHA
#   1 - a phantom, or any condition under which the answer cannot be trusted
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
GH="${GH:-gh}"
PYTHON="${PYTHON:-python3}"
RESOLVER="$REPO_ROOT/creek-tools/scripts/scan_citations.py"
VERIFIER="$REPO_ROOT/creek-tools/scripts/verify-scan-citations.sh"

# Saturates far above any plausible run: a scan files at most `max_issues`
# (default 5) and the whole historical population of the deepest label is 34.
# A listing that comes back exactly this size is treated as truncated.
readonly ISSUE_QUERY_LIMIT=200

readonly LABEL_RE='^scan:[a-z][a-z0-9-]*$'
readonly NON_NEGATIVE_INTEGER_RE='^(0|[1-9][0-9]*)$'

tsv=""
comment=""
# Inline, not a function: a cleanup function invoked only from a trap reads
# as dead code to shellcheck (SC2329), and a suppression comment is not an
# option in this repository.
trap 'rm -f "$tsv" "$comment"' EXIT

# `::error::` on STDOUT: that is the stream the Actions runner reads workflow
# commands from, matching `_claude-scan.yml`'s own guards.
fail() { # fail <message>
  printf '::error::recheck-filed-scan-citations: %s\n' "$1"
  exit 1
}

# --- prove the tools run BEFORE trusting any verdict they produce -----------
[ -f "$RESOLVER" ] ||
  fail "resolver not found at $RESOLVER; refusing to report the filed citations as verified"
[ -x "$VERIFIER" ] ||
  fail "verifier not found or not executable at $VERIFIER; refusing to report the filed citations as verified"
"$PYTHON" -c "import ast, json" >/dev/null 2>&1 ||
  fail "interpreter '$PYTHON' is unusable; refusing to report the filed citations as verified"
command -v "$GH" >/dev/null 2>&1 ||
  fail "'$GH' is not executable; the issues this run filed cannot be read back"

# --- the baseline, which must exist ----------------------------------------
if [ "${ISSUES_SNAPSHOT_OK:-}" != "true" ]; then
  fail "the pre-agent issue snapshot did not succeed (ISSUES_SNAPSHOT_OK='${ISSUES_SNAPSHOT_OK:-<unset>}'); without a baseline every pre-existing issue looks newly filed, so this run is UNVERIFIED rather than clean"
fi

SCAN_LABEL="${SCAN_LABEL:-}"
if ! printf '%s' "$SCAN_LABEL" | grep -qE "$LABEL_RE"; then
  fail "invalid SCAN_LABEL '$SCAN_LABEL' -- must match $LABEL_RE"
fi

# --- what the label holds now ----------------------------------------------
# Built as an array so nothing word-splits, and so `--repo` is either a real
# pair of tokens or absent entirely (`--repo ""` is an error to gh, not a
# no-op). `--state all` because a dedupe pass may have closed an issue this
# run filed, and a closed phantom is still a phantom in the backlog.
query=(issue list --label "$SCAN_LABEL" --state all
  --limit "$ISSUE_QUERY_LIMIT" --json "number,body,createdAt")
if [ -n "${GITHUB_REPOSITORY:-}" ]; then
  query+=(--repo "$GITHUB_REPOSITORY")
fi

payload=""
if ! payload="$("$GH" "${query[@]}")"; then
  fail "could not list '$SCAN_LABEL' issues (gh exited non-zero); a run that cannot read back what it filed must not report the citations verified"
fi

tsv="$(mktemp)"
if ! printf '%s' "$payload" | "$PYTHON" "$RESOLVER" --from-issues \
  --exclude "${ISSUES_BEFORE:-}" \
  --created-after "${RUN_STARTED_AT:-}" \
  --default-sha "${SCAN_SHA:-}" >"$tsv"; then
  fail "could not parse the '$SCAN_LABEL' listing into citations; see the resolver's message above"
fi

returned="$(awk -F'\t' '$1=="RETURNED"{print $2}' "$tsv")"
new_count="$(awk -F'\t' '$1=="ISSUES"{print $2}' "$tsv")"
if [[ ! "$returned" =~ $NON_NEGATIVE_INTEGER_RE ]] ||
  [[ ! "$new_count" =~ $NON_NEGATIVE_INTEGER_RE ]]; then
  fail "the resolver reported no issue counts (returned='$returned', new='$new_count'); a run that cannot count what it filed must not pass"
fi

if [ "$returned" -ge "$ISSUE_QUERY_LIMIT" ]; then
  fail "the '$SCAN_LABEL' listing returned $returned issues at --limit $ISSUE_QUERY_LIMIT, so it may be TRUNCATED; a truncated page drops issues out of the snapshot's set difference and makes an old issue look newly filed. Raise the limit rather than guessing."
fi

if [ "$new_count" -eq 0 ]; then
  printf '%s\n' "recheck-filed-scan-citations: this run filed no $SCAN_LABEL issues; there is nothing to re-verify ($returned pre-existing issue(s) carry the label)."
  exit 0
fi

citation_count="$(awk -F'\t' '$1=="CITATION"' "$tsv" | wc -l | tr -d '[:space:]')"
if [ "$citation_count" -eq 0 ]; then
  fail "this run filed $new_count $SCAN_LABEL issue(s) and not one carries a parseable File(s)/Symbol(s) citation. An unparseable body is not a verified body; fix the issue template usage rather than passing the run."
fi

# --- verify each issue at ITS OWN recorded revision -------------------------
# A `for` over a `while … | read` pipeline: the pipeline runs its body in a
# SUBSHELL, so every increment of `failures` would be discarded and the script
# would exit 0 having found phantoms. Process substitution keeps the loop in
# this shell. No `mapfile`/`readarray`: macOS ships bash 3.2, which is the
# bash the subprocess tests run against under their restricted PATH.
numbers=()
while IFS= read -r number; do
  [ -n "$number" ] && numbers+=("$number")
done < <(awk -F'\t' '$1=="CITATION"{print $2}' "$tsv" | sort -u)

failures=0
for number in ${numbers[@]+"${numbers[@]}"}; do
  sha="$(awk -F'\t' -v n="$number" '$1=="CITATION" && $2==n {print $3; exit}' "$tsv")"
  # The checkout is `fetch-depth: 50`, so a body recording an older SHA may not
  # be present. Say which revision was actually used rather than silently
  # checking a citation against a revision it never claimed.
  if [ -z "$sha" ] || ! git -C "$REPO_ROOT" cat-file -e "${sha}^{commit}" 2>/dev/null; then
    printf '::warning::issue #%s records scan SHA %s, which is not reachable in this checkout; re-checking against %s instead\n' \
      "$number" "'${sha:-<none>}'" "'${SCAN_SHA:-<unset>}'"
    sha="${SCAN_SHA:-}"
  fi

  citations="$(awk -F'\t' -v n="$number" '$1=="CITATION" && $2==n {print $4}' "$tsv")"
  if out="$(printf '%s\n' "$citations" | SCAN_SHA="$sha" "$VERIFIER" 2>&1)"; then
    printf 'issue #%s: %s\n' "$number" "$out"
    continue
  fi

  failures=$((failures + 1))
  names="$(printf '%s\n' "$out" | sed -n "s/.*PHANTOM '\([^']*\)'.*/\1/p" | tr '\n' ' ')"
  printf '::error::issue #%s cites phantom symbol(s) at %s: %s\n' \
    "$number" "${sha:0:12}" "${names:-see the verifier output below}"
  printf '%s\n' "$out"

  # ONE message on the issue, quoting the verifier's own output verbatim: it
  # already carries the phantom name, the SHA and "the lines cited hold 'X'",
  # which is the whole remedy. Paraphrasing it here would be a second, drifting
  # copy of the same text.
  comment="$(mktemp)"
  cat >"$comment" <<COMMENT
**Automated citation re-check failed** (issue #1700).

\`creek-tools/scripts/verify-scan-citations.sh\` re-resolved this issue's
\`Symbol(s):\` / \`File(s):\` citations against the scan SHA \`$sha\`,
independently of the agent that filed it:

\`\`\`
$out
\`\`\`

A phantom citation is a claim about a revision that is not true at that
revision. Re-resolve each name against the scan SHA and correct the body
above, or drop the symbol from the finding (#1651, #1700).
COMMENT
  if ! "$GH" issue comment "$number" --body-file "$comment" >/dev/null 2>&1; then
    printf '::warning::could not comment the correction on issue #%s; the failure above stands regardless\n' "$number"
  fi
  rm -f "$comment"
  comment=""
done

if [ "$failures" -gt 0 ]; then
  fail "$failures of the $new_count issue(s) this run filed cite phantom symbols. Each has been commented with the verifier's own output."
fi

printf '%s\n' "recheck-filed-scan-citations: re-verified $citation_count citation(s) across $new_count newly filed $SCAN_LABEL issue(s)."
exit 0
