#!/usr/bin/env python3
"""Post a Ralph tick-loop recap to Discord whenever a PR is merged.

Reads two environment variables for delivery:

    DISCORD_BOT_TOKEN   Bot token (sent as `Authorization: Bot <token>`).
    RALPH_CHANNEL_ID    Snowflake ID of the channel to post the recap into.

and uses a GitHub token (GITHUB_TOKEN / GH_TOKEN) plus the repo slug
(--repo or $GITHUB_REPOSITORY) to gather the merge history. If ANTHROPIC_API_KEY
is present, the most-recently-merged PR gets a ten-word "what this unlocked"
headline from Claude; otherwise a plain heuristic headline is used.

This is meant to run from a GitHub Actions workflow keyed on
`pull_request: closed` (filtered to merged PRs) — see
`.github/workflows/ralph-recap.yml` — but it runs fine locally too:

    DISCORD_BOT_TOKEN=... RALPH_CHANNEL_ID=... GITHUB_TOKEN=... \
        python scripts/ralph/recap.py --repo Geoffe-Ga/adepthood --dry-run

`--dry-run` prints the rendered embed as JSON instead of posting it.

The backlog count mirrors `scripts/ralph/pick-next.sh`: open issues bearing any
of the picker's exclude labels (epics, blocked, etc.) are not part of Ralph's
real workload, so they are dropped from the ETA. Override the label set with the
same `RALPH_EXCLUDE_LABELS` env var the picker uses.

The statistics math lives in stats.py (pure, unit-tested); this module is the
I/O shell around it.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, cast

import stats

# Optional dependency: only needed for the Claude-written headline. Imported at
# module top level so the rest of the recap works even when it is absent.
try:
    import anthropic as _anthropic_mod
except ImportError:
    _anthropic_mod = None


class RecapError(Exception):
    """A user-facing failure with an associated process exit code."""

    def __init__(self, message: str, code: int = 1) -> None:
        super().__init__(message)
        self.code = code


GITHUB_API = "https://api.github.com"
DISCORD_API = "https://discord.com/api/v10"
# Discord embed accent — a Ralph-purple.
EMBED_COLOR = 0x7C3AED
# Zero-width space — a non-empty Discord field value that renders as blank, used
# to turn a field into a header-only line.
BLANK = "​"
# Section headers. Discord can't size text, so uppercase flanked by four em-dashes
# on each side is what makes these read as section breaks rather than another bold
# field label. No emoji, no window qualifier — just the section name.
_RULE = "—" * 4
THIS_PR_HEADER = f"{_RULE}THIS PR{_RULE}"
LOOP_HEADER = f"{_RULE}THE LOOP{_RULE}"
# The windowed stats (iterations, time-to-merge, tick cadence, busiest day) cover
# this trailing span so they move with recent activity, not a frozen all-time average.
RECENT_WINDOW_DAYS = 7
# Safety cap on PRs analyzed per run: each window PR costs one extra API call
# (its verdicts), so bound a burst day rather than fan out unbounded.
DEFAULT_MAX_PRS = 200
HEADLINE_MODEL = "claude-opus-4-8"

# Issues carrying any of these labels are not part of Ralph's real backlog, so
# they are excluded from the open-issue count behind the ETA. Kept in sync with
# the default exclude set in `scripts/ralph/pick-next.sh`.
DEFAULT_EXCLUDE_LABELS = (
    "epic",
    "wontfix",
    "duplicate",
    "invalid",
    "question",
    "blocked",
    "needs-spec",
    "future-work",
    "do-not-auto-merge",
    "in-progress",
)


# --------------------------------------------------------------------------- #
# HTTP helpers
# --------------------------------------------------------------------------- #


def _request_json(
    url: str,
    *,
    headers: dict[str, str],
    method: str = "GET",
    body: bytes | None = None,
) -> object:
    """Perform an HTTP request and parse a JSON response body.

    Returns the parsed JSON (typed as `object`; callers cast). Raises
    urllib.error.HTTPError / URLError on transport or HTTP failures.
    """
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    with urllib.request.urlopen(request) as response:
        raw = response.read().decode("utf-8")
    return json.loads(raw) if raw else None


def _gh_headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "ralph-recap",
    }


def _gh_get_paged(path: str, *, token: str, params: dict[str, str], max_items: int) -> list[dict[str, Any]]:
    """GET a paginated GitHub list endpoint, stopping at max_items."""
    headers = _gh_headers(token)
    out: list[dict[str, Any]] = []
    page = 1
    while len(out) < max_items:
        query = "&".join(f"{k}={v}" for k, v in {**params, "per_page": "100", "page": str(page)}.items())
        url = f"{GITHUB_API}{path}?{query}"
        chunk = cast("list[dict[str, Any]]", _request_json(url, headers=headers))
        if not chunk:
            break
        out.extend(chunk)
        if len(chunk) < 100:
            break
        page += 1
    return out[:max_items]


# --------------------------------------------------------------------------- #
# GitHub data gathering
# --------------------------------------------------------------------------- #


def _gh_search_issues(query: str, *, token: str, max_items: int) -> list[dict[str, Any]]:
    """Run a GitHub issue/PR search, paging up to max_items results.

    Search responses wrap the hits in an `items` array (unlike the list
    endpoints used by `_gh_get_paged`), so this has its own pager.
    """
    headers = _gh_headers(token)
    out: list[dict[str, Any]] = []
    page = 1
    while len(out) < max_items:
        qs = urllib.parse.urlencode({"q": query, "per_page": "100", "page": str(page)})
        url = f"{GITHUB_API}/search/issues?{qs}"
        data = cast("dict[str, Any]", _request_json(url, headers=headers))
        items = cast("list[dict[str, Any]]", data.get("items", []))
        if not items:
            break
        out.extend(items)
        if len(items) < 100:
            break
        page += 1
    return out[:max_items]


def count_merged_total(repo: str, *, token: str) -> int:
    """Return the true all-time count of merged PRs via the search API.

    Independent of any per-run fetch cap, so the headline total keeps climbing
    instead of pinning at the number of PRs we happened to pull this run.
    """
    qs = urllib.parse.urlencode({"q": f"repo:{repo} is:pr is:merged", "per_page": "1"})
    data = cast("dict[str, Any]", _request_json(f"{GITHUB_API}/search/issues?{qs}", headers=_gh_headers(token)))
    return int(data.get("total_count", 0))


def fetch_recent_merged_prs(repo: str, *, token: str, since: dt.date, max_prs: int) -> list[dict[str, Any]]:
    """Return PRs merged on/after `since` (newest merge first), capped at max_prs.

    Uses search so the window is filtered server-side; each hit carries its
    `pull_request.merged_at` and `created_at`, which is all the windowed stats
    need without a per-PR detail call.
    """
    query = f"repo:{repo} is:pr is:merged merged:>={since.isoformat()}"
    items = _gh_search_issues(query, token=token, max_items=max_prs)
    items.sort(key=lambda it: cast("str", it["pull_request"]["merged_at"]), reverse=True)
    return items


def fetch_pr_detail(repo: str, number: int, *, token: str) -> dict[str, Any]:
    """Fetch a single PR (carries additions/deletions/changed_files)."""
    url = f"{GITHUB_API}/repos/{repo}/pulls/{number}"
    return cast("dict[str, Any]", _request_json(url, headers=_gh_headers(token)))


def _pr_churn(repo: str, number: int, *, token: str) -> tuple[int, int, int]:
    """Return one PR's (additions, deletions, changed_files) from its detail.

    The search/list endpoints omit diff stats, so each PR needs its own detail
    call. A transport failure on a single PR degrades to a zero tuple rather than
    failing the whole recap — the LoC sums simply skip that PR's churn.
    """
    try:
        detail = fetch_pr_detail(repo, number, token=token)
    except (urllib.error.HTTPError, urllib.error.URLError):
        return (0, 0, 0)
    return (
        int(detail.get("additions", 0)),
        int(detail.get("deletions", 0)),
        int(detail.get("changed_files", 0)),
    )


def fetch_pr_verdicts(repo: str, number: int, *, token: str) -> list[str]:
    """Return the ordered list of normalized Claude verdicts on one PR."""
    comments = _gh_get_paged(
        f"/repos/{repo}/issues/{number}/comments",
        token=token,
        params={"sort": "created", "direction": "asc"},
        max_items=100,
    )
    verdicts: list[str] = []
    for comment in comments:
        verdict = stats.normalize_verdict(str(comment.get("body", "")))
        if verdict is not None:
            verdicts.append(verdict)
    return verdicts


def fetch_repo_net_lines(repo: str, *, token: str, attempts: int = 3) -> int | None:
    """Return net lines of code across the whole repo via the code-frequency stats API.

    GitHub computes the per-week additions/deletions stats asynchronously and
    answers 202 with an empty body on a cold cache, so retry a few times. Any
    failure (still warming, or an HTTP/transport error) returns None so the recap
    shows a placeholder for the full-repo figure instead of failing outright.
    """
    url = f"{GITHUB_API}/repos/{repo}/stats/code_frequency"
    for _ in range(attempts):
        try:
            data = _request_json(url, headers=_gh_headers(token))
        except (urllib.error.HTTPError, urllib.error.URLError):
            return None
        if data:
            return stats.net_lines_from_code_frequency(cast("list[list[int]]", data))
    return None


def _excluded_labels() -> set[str]:
    """Return the label set that disqualifies an issue from the backlog count.

    Mirrors `scripts/ralph/pick-next.sh`: defaults to the housekeeping/deferred
    markers, overridable via the same `RALPH_EXCLUDE_LABELS` env var (a
    space-separated list). An empty override counts every open issue.
    """
    raw = os.environ.get("RALPH_EXCLUDE_LABELS")
    if raw is None:
        return set(DEFAULT_EXCLUDE_LABELS)
    return {label for label in raw.split() if label}


def count_open_backlog(repo: str, *, token: str, max_items: int = 1000) -> int:
    """Count open issues that are real, unblocked Ralph work — the backlog.

    Excludes pull requests (which the issues endpoint returns too) and any issue
    bearing an excluded label, so the ETA reflects what the picker would
    actually pick up rather than every open card.
    """
    issues = _gh_get_paged(
        f"/repos/{repo}/issues",
        token=token,
        params={"state": "open"},
        max_items=max_items,
    )
    excluded = _excluded_labels()
    backlog = 0
    for issue in issues:
        if "pull_request" in issue:
            continue
        names = {str(label.get("name", "")) for label in issue.get("labels", [])}
        if names & excluded:
            continue
        backlog += 1
    return backlog


# --------------------------------------------------------------------------- #
# Headline generation
# --------------------------------------------------------------------------- #


def _heuristic_headline(title: str) -> str:
    """Fallback ten-word headline: the cleaned PR title, clipped to ten words."""
    cleaned = title.strip()
    for prefix in ("feat:", "feat(", "fix:", "chore:", "refactor:", "docs:"):
        if cleaned.lower().startswith(prefix):
            # split on the first ":" if present; a prefix with no colon (rare)
            # leaves the title unchanged since split returns the whole string.
            cleaned = cleaned.split(":", 1)[-1].strip()
            break
    words = cleaned.split()
    return " ".join(words[:10]) if words else "Latest change merged into the tick loop"


def generate_headline(title: str, body: str) -> str:
    """Ask Claude for a ten-word "what this unlocked" headline.

    Falls back to a heuristic if the Anthropic SDK or API key is unavailable, so
    the recap never fails just because the headline can't be generated.
    """
    if _anthropic_mod is None or not os.environ.get("ANTHROPIC_API_KEY"):
        return _heuristic_headline(title)

    prompt = (
        "A pull request was just merged into an autonomous coding loop. "
        "Write a single headline of at most ten words describing what merging "
        "this PR has unlocked or newly made possible for the project. Lead with "
        "the capability or outcome, not the implementation. Plain language only: "
        "no buzzwords, no 'leverage'/'robust'/'seamless'/'synergy', no jargon "
        "soup, no trailing punctuation. Return only the headline.\n\n"
        f"PR title: {title}\n\n"
        f"PR description:\n{body[:4000]}"
    )
    try:
        client = _anthropic_mod.Anthropic()
        response = client.messages.create(
            model=HEADLINE_MODEL,
            max_tokens=256,
            output_config={"effort": "low"},
            messages=[{"role": "user", "content": prompt}],
        )
        text = "".join(block.text for block in response.content if block.type == "text").strip()
    except Exception:  # any SDK/API failure degrades to the heuristic, never fails the recap
        return _heuristic_headline(title)
    return text or _heuristic_headline(title)


# --------------------------------------------------------------------------- #
# Recap assembly
# --------------------------------------------------------------------------- #


def _fmt_hours(hours: float) -> str:
    if hours < 1:
        return f"{round(hours * 60)}m"
    if hours < 48:
        return f"{hours:.1f}h"
    return f"{hours / 24:.1f}d"


def _fmt_eta(estimate: dict[str, object]) -> str:
    if not estimate["known"]:
        return "unknown (loop is stalled — no recent merges)"
    days = cast("float | None", estimate["days_remaining"])
    if days is None or days <= 0:
        return "backlog clear 🎉"
    eta = cast("dt.datetime", estimate["eta"])
    return f"~{days:.1f} days (≈ {eta.date().isoformat()})"


def _pr_merged_at(pr: dict[str, Any]) -> dt.datetime:
    """Parse a search hit's merge timestamp from its `pull_request` block."""
    return stats.parse_iso(cast("str", pr["pull_request"]["merged_at"]))


def _open_to_merge_hours(pr: dict[str, Any]) -> float:
    """Hours from a PR opening to its merge — the review/merge window.

    Clamped to zero so clock skew can never produce a negative duration. A PR's
    commit timestamps are deliberately not used here: for single-commit squash
    PRs the commit is authored seconds before the PR opens, so "first commit ->
    merge" collapses to this same window. The genuine end-to-end work duration is
    captured by the merge-to-merge tick cadence instead (see
    `stats.merge_intervals_hours`).
    """
    merged = _pr_merged_at(pr)
    opened = stats.parse_iso(cast("str", pr["created_at"]))
    return max((merged - opened).total_seconds() / 3600.0, 0.0)


def build_recap(repo: str, *, token: str, max_prs: int, now: dt.datetime) -> dict[str, Any] | None:
    """Gather data and assemble the Discord embed payload.

    The headline total is the true all-time merged count; the activity and
    quality stats (rate, iterations, time-to-merge, tick cadence, busiest day)
    cover the trailing `RECENT_WINDOW_DAYS` so they move with recent work. Returns
    None when there are no merged PRs yet (nothing to recap).
    """
    total_merged = count_merged_total(repo, token=token)
    since = (now - dt.timedelta(days=RECENT_WINDOW_DAYS)).date()
    window = fetch_recent_merged_prs(repo, token=token, since=since, max_prs=max_prs)
    if not window:
        return None

    merged_at = [_pr_merged_at(pr) for pr in window]

    # One pass over the window collects both review rounds (from verdicts) and
    # churn (from PR detail) per PR. window[0] is the just-merged PR, so its own
    # figures fall out of index 0 without a second fetch.
    iterations: list[int] = []
    churn: list[tuple[int, int, int]] = []
    latest_iterations: int | None = None
    for index, pr in enumerate(window):
        verdicts = fetch_pr_verdicts(repo, int(pr["number"]), token=token)
        rounds = stats.iterations_before_lgtm(verdicts)
        if rounds is not None:
            iterations.append(rounds)
        if index == 0:
            latest_iterations = rounds
        churn.append(_pr_churn(repo, int(pr["number"]), token=token))

    open_to_merge = [_open_to_merge_hours(pr) for pr in window]
    intervals = stats.merge_intervals_hours(merged_at)

    latest = window[0]

    rate = stats.merge_rate(merged_at, now=now)
    ttm = stats.time_to_merge_stats(open_to_merge)
    tick = stats.time_to_merge_stats(intervals)
    iters = stats.iteration_stats(iterations)
    open_items = count_open_backlog(repo, token=token)
    estimate = stats.estimate_remaining(open_items, rate["per_day"], now=now)
    busy = stats.busiest_day(merged_at)

    # Footprint is the just-merged PR; the loop's LoC sums the 7d window (the whole
    # fetched set) and the 24h slice of it, with the full-repo net from the stats API.
    day_ago = now - dt.timedelta(hours=stats.HOURS_PER_DAY)
    latest_churn = stats.churn_totals(churn[:1])
    loc_7d = stats.churn_totals(churn)
    loc_24h = stats.churn_totals([c for c, ts in zip(churn, merged_at) if ts >= day_ago])
    repo_net = fetch_repo_net_lines(repo, token=token)

    headline = generate_headline(str(latest.get("title", "")), str(latest.get("body") or ""))

    # Per-PR (this-merge) figures, kept distinct from the windowed dataset above.
    latest_ttm_hours = _open_to_merge_hours(latest)
    latest_tick_hours = (merged_at[0] - merged_at[1]).total_seconds() / 3600.0 if len(merged_at) >= 2 else None

    return _render_embed(
        repo=repo,
        latest=latest,
        headline=headline,
        total_merged=total_merged,
        rate=rate,
        ttm=ttm,
        tick=tick,
        iters=iters,
        estimate=estimate,
        latest_churn=latest_churn,
        loc_24h=loc_24h,
        loc_7d=loc_7d,
        repo_net=repo_net,
        busy=busy,
        latest_iterations=latest_iterations,
        latest_ttm_hours=latest_ttm_hours,
        latest_tick_hours=latest_tick_hours,
        now=now,
    )


def _stat_line(summary: dict[str, float]) -> str:
    """Render a median/fastest/slowest duration summary, or a gap-data notice."""
    if summary["slowest"] <= 0:
        return "not enough merges in the window yet"
    return (
        f"median **{_fmt_hours(summary['median'])}** · "
        f"fastest {_fmt_hours(summary['fastest'])} · slowest {_fmt_hours(summary['slowest'])}"
    )


def _this_pr_review_line(latest_ttm_hours: float) -> str:
    """The just-merged PR's own open → merge review window."""
    return f"**{_fmt_hours(latest_ttm_hours)}** open → merge"


def _this_pr_tick_line(latest_tick_hours: float | None) -> str:
    """The just-merged PR's full tick — the gap since the previous merge."""
    if latest_tick_hours is None:
        return "first tracked merge"
    return f"**{_fmt_hours(latest_tick_hours)}** since the previous merge"


def _this_pr_iter_line(latest_iterations: int | None) -> str:
    """Review rounds the just-merged PR took before its first LGTM verdict."""
    if latest_iterations is None:
        return "merged without an LGTM verdict"
    if latest_iterations == 0:
        return "**0** rounds · clean first try"
    plural = "round" if latest_iterations == 1 else "rounds"
    return f"**{latest_iterations}** {plural} to LGTM"


def _loc_line(loc_24h: dict[str, int], loc_7d: dict[str, int], repo_net: int | None) -> str:
    """Lines-of-code churn over the 24h and 7d windows, plus the full-repo net."""

    def churn(totals: dict[str, int]) -> str:
        return f"+{totals['additions']:,} / -{totals['deletions']:,}"

    full_repo = f"{repo_net:,} net" if repo_net is not None else "—"
    return f"{churn(loc_24h)} (24h) · {churn(loc_7d)} (7d) · {full_repo} (full repo)"


def _render_embed(
    *,
    repo: str,
    latest: dict[str, Any],
    headline: str,
    total_merged: int,
    rate: dict[str, float],
    ttm: dict[str, float],
    tick: dict[str, float],
    iters: dict[str, float],
    estimate: dict[str, object],
    latest_churn: dict[str, int],
    loc_24h: dict[str, int],
    loc_7d: dict[str, int],
    repo_net: int | None,
    busy: tuple[str, int] | None,
    latest_iterations: int | None,
    latest_ttm_hours: float,
    latest_tick_hours: float | None,
    now: dt.datetime,
) -> dict[str, Any]:
    """Turn computed stats into a Discord embed payload (one message).

    Fields are split into two labelled blocks: the just-merged PR ("This PR")
    and the rolling/all-time loop figures ("The loop"), so a single merge's
    numbers are never confused with the dataset-wide ones. Within each block,
    multi-window stats run smallest window first (24h → 7d → all-time).
    """
    pr_number = int(latest["number"])
    pr_url = str(latest.get("html_url", f"https://github.com/{repo}/pull/{pr_number}"))

    clean_pct = round(iters["clean_merge_rate"] * 100)
    iter_line = (
        f"**{iters['mean']:.1f}** avg rounds to LGTM · "
        f"**{clean_pct}%** first-try clean · "
        f"worst **{int(iters['max'])}** (n={int(iters['sample'])})"
        if iters["sample"]
        else "no LGTM verdicts in the last 7d"
    )

    busy_line = f"{busy[1]} merges on {busy[0]}" if busy else "—"
    footprint = f"+{latest_churn['additions']} / -{latest_churn['deletions']} across {latest_churn['files']} file(s)"

    fields = [
        # A blank field is a Discord spacer; it puts a padding line above each
        # section header so the blocks breathe instead of butting up against the
        # field before them.
        {"name": BLANK, "value": BLANK, "inline": False},
        {"name": THIS_PR_HEADER, "value": BLANK, "inline": False},
        {"name": "🔓 Unlock", "value": f"*{headline}*", "inline": False},
        {"name": "🔗 Link", "value": f"[#{pr_number} — {latest.get('title', '')}]({pr_url})", "inline": False},
        {"name": "🧮 Footprint", "value": footprint, "inline": True},
        {"name": "🔁 Review iterations", "value": _this_pr_iter_line(latest_iterations), "inline": True},
        {"name": "⏱️ Time for review", "value": _this_pr_review_line(latest_ttm_hours), "inline": True},
        {"name": "🔄 Tick length", "value": _this_pr_tick_line(latest_tick_hours), "inline": True},
        {"name": BLANK, "value": BLANK, "inline": False},
        {"name": LOOP_HEADER, "value": BLANK, "inline": False},
        {"name": "🔥 Busiest day (7d)", "value": busy_line, "inline": True},
        {
            "name": "📦 PRs merged",
            "value": f"{int(rate['last_24h'])} in 24h · {int(rate['last_7_days'])} in 7d · **{total_merged}** all-time",
            "inline": True,
        },
        {"name": "📈 LoC", "value": _loc_line(loc_24h, loc_7d, repo_net), "inline": False},
        {
            "name": "⚡ Merge rate",
            "value": f"**{rate['per_hour']:.2f}**/hr (24h) · {rate['per_day']:.1f}/day (7d)",
            "inline": True,
        },
        {"name": "🔁 Review iterations (7d)", "value": iter_line, "inline": False},
        {"name": "⏱️ Time for review (7d)", "value": _stat_line(ttm), "inline": False},
        {"name": "🔄 Tick length (7d)", "value": _stat_line(tick), "inline": False},
        {
            "name": "🗺️ Backlog remaining",
            "value": f"**{estimate['open_items']}** open · ETA {_fmt_eta(estimate)}",
            "inline": True,
        },
    ]

    embed = {
        "title": f"🤖 Ralph Recap — {repo}",
        "url": f"https://github.com/{repo}/pulls?q=is%3Apr+is%3Amerged",
        "description": f"Another tick landed. Here's where the loop stands as of <t:{int(now.timestamp())}:R>.",
        "color": EMBED_COLOR,
        "fields": fields,
        "footer": {"text": "Ralph tick loop · recap fires on every merge"},
        "timestamp": now.isoformat(),
    }
    return {"embeds": [embed]}


# --------------------------------------------------------------------------- #
# Discord delivery
# --------------------------------------------------------------------------- #


def post_to_discord(channel_id: str, token: str, payload: dict[str, Any]) -> None:
    """Post the recap payload to a Discord channel as a bot."""
    url = f"{DISCORD_API}/channels/{channel_id}/messages"
    headers = {
        "Authorization": f"Bot {token}",
        "Content-Type": "application/json",
        "User-Agent": "DiscordBot (ralph-recap, 1.0)",
    }
    _request_json(url, headers=headers, method="POST", body=json.dumps(payload).encode("utf-8"))


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


def _gather(repo: str, gh_token: str, max_prs: int) -> dict[str, Any] | None:
    """Build the recap payload, mapping network failures to RecapError."""
    now = dt.datetime.now(dt.timezone.utc)
    try:
        return build_recap(repo, token=gh_token, max_prs=max_prs, now=now)
    except urllib.error.HTTPError as exc:
        raise RecapError(f"GitHub API request failed: {exc.code} {exc.reason}") from exc
    except urllib.error.URLError as exc:
        raise RecapError(f"network failure talking to GitHub: {exc.reason}") from exc


def _deliver(channel_id: str | None, payload: dict[str, Any]) -> None:
    """Post the payload to Discord, mapping failures to RecapError."""
    if not channel_id:
        raise RecapError("RALPH_CHANNEL_ID (or --channel-id) is required to post", code=2)
    discord_token = os.environ.get("DISCORD_BOT_TOKEN")
    if not discord_token:
        raise RecapError("DISCORD_BOT_TOKEN is required to post", code=2)
    try:
        post_to_discord(channel_id, discord_token, payload)
    except urllib.error.HTTPError as exc:
        raise RecapError(f"Discord API request failed: {exc.code} {exc.reason}") from exc
    except urllib.error.URLError as exc:
        raise RecapError(f"network failure talking to Discord: {exc.reason}") from exc


def _run(args: argparse.Namespace) -> int:
    if not args.repo:
        raise RecapError("--repo or $GITHUB_REPOSITORY is required", code=2)
    gh_token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if not gh_token:
        raise RecapError("GITHUB_TOKEN (or GH_TOKEN) is required", code=2)

    payload = _gather(str(args.repo), gh_token, int(args.max_prs))
    if payload is None:
        print("No merged PRs yet — nothing to recap.")
        return 0
    if args.dry_run:
        print(json.dumps(payload, indent=2))
        return 0

    _deliver(args.channel_id, payload)
    print(f"Posted Ralph recap to channel {args.channel_id}.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY"), help="owner/repo slug")
    parser.add_argument("--channel-id", default=os.environ.get("RALPH_CHANNEL_ID"))
    parser.add_argument("--max-prs", type=int, default=DEFAULT_MAX_PRS)
    parser.add_argument("--dry-run", action="store_true", help="print the embed instead of posting")
    args = parser.parse_args(argv)
    try:
        return _run(args)
    except RecapError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return exc.code


if __name__ == "__main__":
    sys.exit(main())
