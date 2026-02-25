#!/usr/bin/env bash
# scripts/pr-status.sh - GitHub Actions workflow monitor for PRs
# Usage: ./scripts/pr-status.sh <subcommand> [OPTIONS]

set -euo pipefail

VERBOSE=false

# Auto-detect repo
get_repo() {
    gh repo view --json nameWithOwner --jq '.nameWithOwner'
}

# Print usage
usage() {
    cat << EOF
Usage: $(basename "$0") <subcommand> [OPTIONS]

GitHub Actions workflow monitor for PRs.

SUBCOMMANDS:
    list [--branch NAME] [--limit N]    List recent CI workflow runs
    view ID [ID...]                      View workflow run conclusions
    watch ID [ID...]                     Watch runs until complete
    checks PR_NUMBER                     Show PR check status
    status PR_NUMBER [--workflow FILE]   Full PR verdict (CI + Claude review)

OPTIONS:
    --verbose   Show detailed output
    --help      Display this help message

STATUS OPTIONS:
    --workflow FILE   CI workflow filename (default: ci.yml)

EXIT CODES:
    0           Success / ready to merge
    1           Failure / not ready to merge
    2           Usage error

EXAMPLES:
    $(basename "$0") list                    # List recent CI runs
    $(basename "$0") list --branch feat/foo  # Filter by branch
    $(basename "$0") view 12345              # View run #12345
    $(basename "$0") watch 12345 12346       # Watch two runs
    $(basename "$0") checks 74               # Show PR #74 checks
    $(basename "$0") status 74               # Full PR #74 verdict
EOF
}

# === list subcommand ===
cmd_list() {
    local branch=""
    local limit=10

    while [[ $# -gt 0 ]]; do
        case $1 in
            --branch)
                branch="$2"
                shift 2
                ;;
            --limit)
                if ! [[ "$2" =~ ^[0-9]+$ ]]; then
                    echo "Error: --limit requires a numeric value, got '$2'" >&2
                    exit 2
                fi
                limit="$2"
                shift 2
                ;;
            *)
                echo "Error: Unknown option for list: $1" >&2
                exit 2
                ;;
        esac
    done

    local repo
    repo="$(get_repo)"

    echo "=== Recent CI Workflow Runs ==="
    echo ""

    local args=(run list --repo "$repo" --limit "$limit")
    if [[ -n "$branch" ]]; then
        args+=(--branch "$branch")
    fi
    args+=(--json "databaseId,headBranch,workflowName,status,conclusion,createdAt")
    args+=(--jq '.[] | [(.databaseId|tostring), .headBranch, .workflowName, .status, (.conclusion // "—"), .createdAt] | @tsv')

    if $VERBOSE; then
        echo "Running: gh ${args[*]}"
    fi

    printf "%-12s %-30s %-20s %-12s %-18s %s\n" "ID" "BRANCH" "WORKFLOW" "STATUS" "CONCLUSION" "CREATED"
    printf "%-12s %-30s %-20s %-12s %-18s %s\n" "----" "------" "--------" "------" "----------" "-------"

    gh "${args[@]}" | while IFS=$'\t' read -r id branch_name workflow status conclusion created; do
        printf "%-12s %-30s %-20s %-12s %-18s %s\n" "$id" "$branch_name" "$workflow" "$status" "$conclusion" "$created"
    done
}

# === view subcommand ===
cmd_view() {
    if [[ $# -eq 0 ]]; then
        echo "Error: view requires at least one run ID" >&2
        exit 2
    fi

    local repo
    repo="$(get_repo)"
    local any_failed=false

    for run_id in "$@"; do
        echo "=== Run #${run_id} ==="
        echo ""

        local run_json
        run_json="$(gh run view "$run_id" --repo "$repo" --json "status,conclusion,workflowName,headBranch,jobs")"

        if $VERBOSE; then
            echo "Fetched run data for #${run_id}"
        fi

        local status conclusion workflow branch
        read -r workflow branch status conclusion < <(
            echo "$run_json" | jq -r '[.workflowName, .headBranch, .status, (.conclusion // "—")] | @tsv'
        )

        echo "Workflow:   $workflow"
        echo "Branch:     $branch"
        echo "Status:     $status"
        echo "Conclusion: $conclusion"
        echo ""

        echo "Jobs:"
        echo "$run_json" | jq -r '.jobs[] | "  \(if .conclusion == "success" then "✓" elif .conclusion == "failure" then "✗" elif .conclusion == "skipped" then "—" else "●" end) \(.name): \(.conclusion // .status)"'
        echo ""

        if [[ "$conclusion" == "failure" ]]; then
            any_failed=true
        fi
    done

    if $any_failed; then
        exit 1
    fi
}

# === watch subcommand ===
cmd_watch() {
    if [[ $# -eq 0 ]]; then
        echo "Error: watch requires at least one run ID" >&2
        exit 2
    fi

    local repo
    repo="$(get_repo)"
    local any_failed=false

    for run_id in "$@"; do
        echo "=== Watching Run #${run_id} ==="
        echo ""

        if $VERBOSE; then
            echo "Watching run #${run_id} in repo $repo"
        fi

        if ! gh run watch "$run_id" --repo "$repo" --exit-status; then
            any_failed=true
            echo "✗ Run #${run_id} failed" >&2
        else
            echo "✓ Run #${run_id} passed"
        fi
        echo ""
    done

    if $any_failed; then
        exit 1
    fi
}

# === checks subcommand ===
cmd_checks() {
    if [[ $# -eq 0 ]]; then
        echo "Error: checks requires a PR number" >&2
        exit 2
    fi

    local pr_number="$1"
    local repo
    repo="$(get_repo)"

    echo "=== PR #${pr_number} Checks ==="
    echo ""

    if $VERBOSE; then
        echo "Fetching checks for PR #${pr_number} in repo $repo"
    fi

    gh pr checks "$pr_number" --repo "$repo"
}

# === status subcommand ===
cmd_status() {
    if [[ $# -eq 0 ]]; then
        echo "Error: status requires a PR number" >&2
        exit 2
    fi

    local pr_number="$1"
    shift
    local workflow="ci.yml"

    while [[ $# -gt 0 ]]; do
        case $1 in
            --workflow)
                workflow="$2"
                shift 2
                ;;
            *)
                echo "Error: Unknown option for status: $1" >&2
                exit 2
                ;;
        esac
    done

    local repo
    repo="$(get_repo)"

    # Get PR info
    local pr_json
    pr_json="$(gh pr view "$pr_number" --repo "$repo" --json "title,headRefName,comments")"

    local pr_title pr_branch
    pr_title="$(echo "$pr_json" | jq -r '.title')"
    pr_branch="$(echo "$pr_json" | jq -r '.headRefName')"

    echo "=== PR #${pr_number}: ${pr_title} ==="
    echo ""

    # --- CI Status ---
    local ci_status="UNKNOWN"
    local ci_detail=""
    local ci_pass=false

    local run_json
    if $VERBOSE; then
        echo "Looking for workflow: $workflow on branch: $pr_branch"
    fi

    run_json="$(gh run list --repo "$repo" --branch "$pr_branch" --workflow "$workflow" --limit 1 --json "databaseId,conclusion,status" 2>/dev/null || echo "[]")"

    local run_count
    run_count="$(echo "$run_json" | jq 'length')"

    if [[ "$run_count" -eq 0 ]]; then
        ci_status="NO RUNS"
        ci_detail="No CI runs found for branch $pr_branch"
        echo "Warning: No runs found for workflow '$workflow' on branch '$pr_branch'." >&2
        echo "  Check workflow filename or use --workflow <file> to specify." >&2
    else
        local run_id run_conclusion run_status
        run_id="$(echo "$run_json" | jq -r '.[0].databaseId')"
        run_conclusion="$(echo "$run_json" | jq -r '.[0].conclusion // ""')"
        run_status="$(echo "$run_json" | jq -r '.[0].status')"

        if [[ "$run_status" != "completed" ]]; then
            ci_status="IN PROGRESS"
            ci_detail="Run #${run_id} is ${run_status}"
        else
            local jobs_json
            jobs_json="$(gh run view "$run_id" --repo "$repo" --json jobs)"

            local total_jobs passed_jobs failed_jobs
            total_jobs="$(echo "$jobs_json" | jq '.jobs | length')"
            passed_jobs="$(echo "$jobs_json" | jq '[.jobs[] | select(.conclusion == "success")] | length')"
            failed_jobs="$(echo "$jobs_json" | jq '[.jobs[] | select(.conclusion == "failure")] | length')"

            if [[ "$run_conclusion" == "success" ]]; then
                ci_status="PASS"
                ci_detail="${passed_jobs}/${total_jobs} jobs green"
                ci_pass=true
            else
                ci_status="FAIL"
                ci_detail="${passed_jobs}/${total_jobs} jobs green, ${failed_jobs} failed"

                # Show failed jobs
                local failed_names
                failed_names="$(echo "$jobs_json" | jq -r '.jobs[] | select(.conclusion == "failure") | .name')"
                if [[ -n "$failed_names" ]]; then
                    ci_detail="${ci_detail}"$'\n'"Failed jobs:"
                    while IFS= read -r name; do
                        ci_detail="${ci_detail}"$'\n'"  ✗ ${name}"
                    done <<< "$failed_names"
                fi
            fi
        fi
    fi

    # --- Claude Review Status ---
    local review_status="NO REVIEW"
    local review_issues=""
    local review_pass=false

    # Scan comments for Claude review verdicts (latest wins)
    local comments_count
    comments_count="$(echo "$pr_json" | jq '.comments | length')"

    if [[ "$comments_count" -gt 0 ]]; then
        # Search from latest comment backwards for a verdict
        local i
        for ((i = comments_count - 1; i >= 0; i--)); do
            local body
            body="$(echo "$pr_json" | jq -r ".comments[$i].body")"

            # Check for verdict patterns (handles bold markdown: **LGTM**)
            if echo "$body" | grep -qE '✅\s*\*{0,2}LGTM|Verdict.*LGTM'; then
                review_status="LGTM"
                review_pass=true
                break
            elif echo "$body" | grep -qE '🔄\s*\*{0,2}CHANGES_REQUESTED|Verdict.*CHANGES_REQUESTED'; then
                review_status="CHANGES_REQUESTED"

                # Extract problems section
                review_issues="$(echo "$body" | sed -n '/^## Problems/,/^## [^P]/p' | sed '$d')"
                if [[ -z "$review_issues" ]]; then
                    # Try alternate format: lines starting with 🔴
                    review_issues="$(echo "$body" | grep '🔴' || true)"
                fi
                break
            elif echo "$body" | grep -qE '💬\s*\*{0,2}COMMENTS|Verdict.*COMMENTS'; then
                review_status="COMMENTS"
                review_pass=true
                break
            fi
        done
    fi

    # --- Output ---
    if $ci_pass; then
        echo "CI Status:     ✓ ${ci_status}  (${ci_detail})"
    else
        echo "CI Status:     ✗ ${ci_status}  (${ci_detail})"
    fi

    if $review_pass; then
        echo "Claude Review: ✓ ${review_status}"
    else
        echo "Claude Review: ✗ ${review_status}"
    fi

    if [[ -n "$review_issues" ]]; then
        echo ""
        echo "Review Issues:"
        echo "$review_issues" | while IFS= read -r line; do
            # Indent if not already indented
            if [[ "$line" == "  "* ]]; then
                echo "$line"
            else
                echo "  $line"
            fi
        done
    fi

    echo ""

    # --- Verdict ---
    if $ci_pass && $review_pass; then
        echo "Verdict: READY TO MERGE"
        exit 0
    else
        echo "Verdict: NOT READY TO MERGE"
        exit 1
    fi
}

# === Main argument parsing ===

# Handle no arguments
if [[ $# -eq 0 ]]; then
    usage
    exit 2
fi

# Extract global flags first, collect remaining args
REMAINING_ARGS=()
while [[ $# -gt 0 ]]; do
    case $1 in
        --verbose)
            VERBOSE=true
            shift
            ;;
        --help|-h)
            usage
            exit 0
            ;;
        *)
            REMAINING_ARGS+=("$1")
            shift
            ;;
    esac
done

# Restore positional args
set -- "${REMAINING_ARGS[@]+"${REMAINING_ARGS[@]}"}"

if [[ $# -eq 0 ]]; then
    usage
    exit 2
fi

SUBCOMMAND="$1"
shift

case "$SUBCOMMAND" in
    list)
        cmd_list "$@"
        ;;
    view)
        cmd_view "$@"
        ;;
    watch)
        cmd_watch "$@"
        ;;
    checks)
        cmd_checks "$@"
        ;;
    status)
        cmd_status "$@"
        ;;
    *)
        echo "Error: Unknown subcommand: $SUBCOMMAND" >&2
        echo "Run '$(basename "$0") --help' for usage." >&2
        exit 2
        ;;
esac
