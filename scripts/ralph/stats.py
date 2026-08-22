#!/usr/bin/env python3
"""Pure statistics helpers for the Ralph tick-loop recap.

Everything in this module is side-effect free: it takes already-fetched data
(lists of timestamps, verdict sequences, churn tuples) and returns plain
dicts/numbers. `recap.py` does all the GitHub / Discord / Anthropic I/O and
hands the data here. Keeping the math pure makes it unit-testable without a
network.

A "Ralph tick loop" is an autonomous agent that grinds a backlog one issue at
a time: each tick opens a PR, iterates against the Claude reviewer's verdict
until LGTM, merges, and moves to the next backlog item. These helpers turn the
merge history into the numbers a human wants to see in a recap.
"""

from __future__ import annotations

import datetime as dt
import re
from collections import Counter

# A Claude reviewer verdict, normalized. The reviewer posts a comment ending in
# a `Verdict:` line; we collapse it to one of these three tokens.
LGTM = "LGTM"
CHANGES_REQUESTED = "CHANGES_REQUESTED"
COMMENTS = "COMMENTS"

# Anchors a genuine verdict LINE (heading- and bold-prefix tolerant) so a stray
# mention of "verdict" in prose does not false-positive, and captures the token
# the line itself states. The `\W*` bridge tolerates markdown/emoji decoration
# between the anchor and the token ("**Verdict:** LGTM", "## Verdict\n✅ LGTM")
# but no words, so prose after "Verdict" cannot smuggle in a token from later
# in the sentence. The line anchor must be kept in sync with pr-ready.sh's
# VERDICT_RE (this pattern additionally captures the token); single backslashes
# here because — unlike pr-ready.sh — this pattern is not spliced into a jq
# string literal.
_VERDICT_LINE_RE = re.compile(
    r"^\s*(?:#{1,6}\s+|\*\*)?verdict[:*\s]\W*(lgtm|changes[_ ]requested|comments)\b",
    re.IGNORECASE | re.MULTILINE,
)

ISO_NO_TZ = "%Y-%m-%dT%H:%M:%SZ"


def parse_iso(timestamp: str) -> dt.datetime:
    """Parse a GitHub ISO-8601 timestamp into an aware UTC datetime.

    GitHub stamps end in `Z`, which `datetime.fromisoformat` accepts natively
    on the 3.11+ interpreters this repo's tooling pins.
    """
    return dt.datetime.fromisoformat(timestamp)


def normalize_verdict(raw: str) -> str | None:
    """Collapse a Claude review comment body to a single verdict token.

    Returns None when no line states a genuine verdict, so callers can ignore
    non-review comments (a stray "verdict" mentioned mid-prose, or a
    "Verdict"-led sentence that never states a token, does not count). The
    token is read from the verdict line itself — never by substring-scanning
    the rest of the body, where a rationale that merely mentions "LGTM" or
    "CHANGES_REQUESTED" in prose would flip the classification and skew the
    recap's Review-iterations metric. When several verdict lines exist (the
    reviewer re-verdicted after another pass), the LAST one wins.
    """
    tokens: list[str] = _VERDICT_LINE_RE.findall(raw)
    if not tokens:
        return None
    return tokens[-1].upper().replace(" ", "_")


def iterations_before_lgtm(verdicts: list[str]) -> int | None:
    """Count review rounds a PR took before its first LGTM.

    `verdicts` is the ordered list of normalized verdicts for one PR. The result
    is the number of non-LGTM verdicts that preceded the first LGTM — i.e. how
    many times the loop had to go back and address feedback. Returns None if the
    PR never reached an LGTM verdict (it was merged on human judgment, or the
    reviewer only left COMMENTS), so it can be excluded from the average.
    """
    for index, verdict in enumerate(verdicts):
        if verdict == LGTM:
            return index
    return None


def _mean(values: list[float]) -> float:
    """Mean."""
    return sum(values) / len(values) if values else 0.0


def _median(values: list[float]) -> float:
    """Median."""
    if not values:
        return 0.0
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2


HOURS_PER_DAY = 24.0
DAYS_PER_WEEK = 7.0


def merge_rate(merged_at: list[dt.datetime], *, now: dt.datetime) -> dict[str, float]:
    """Compute merge throughput over fixed rolling windows.

    Reports activity over the trailing 24 hours (as merges-per-hour) and the
    trailing 7 days (as merges-per-day). Fixed-width windows mean every recap
    moves with the latest data instead of being diluted by a frozen all-time
    span, and an idle loop decays toward zero rather than showing a stale
    average. The 7-day per-day figure (steadier than the 24h one) is what the
    backlog ETA is built on.
    """
    if not merged_at:
        return {"last_24h": 0.0, "per_hour": 0.0, "last_7_days": 0.0, "per_day": 0.0}

    day_ago = now - dt.timedelta(hours=HOURS_PER_DAY)
    week_ago = now - dt.timedelta(days=DAYS_PER_WEEK)
    last_24h = sum(1 for ts in merged_at if ts >= day_ago)
    last_7 = sum(1 for ts in merged_at if ts >= week_ago)

    return {
        "last_24h": float(last_24h),
        "per_hour": last_24h / HOURS_PER_DAY,
        "last_7_days": float(last_7),
        "per_day": last_7 / DAYS_PER_WEEK,
    }


def time_to_merge_stats(durations_hours: list[float]) -> dict[str, float]:
    """Summarize a list of PR durations (hours) as mean/median/fastest/slowest.

    Used for both the open-to-merge window and the merge-to-merge tick cadence —
    it is just a five-number summary over whatever durations it is handed.
    """
    if not durations_hours:
        return {"mean": 0.0, "median": 0.0, "fastest": 0.0, "slowest": 0.0}
    return {
        "mean": _mean(durations_hours),
        "median": _median(durations_hours),
        "fastest": min(durations_hours),
        "slowest": max(durations_hours),
    }


def merge_intervals_hours(merged_at: list[dt.datetime]) -> list[float]:
    """Hours between each consecutive pair of merges — the per-tick cadence.

    Sorts the merge timestamps ascending and returns the gap, in hours, between
    each merge and the one before it (so n merges yield n-1 intervals; fewer than
    two merges yield an empty list). For a sequential loop this is the closest
    available proxy for how long each tick actually took end to end — pick, code,
    review, merge — because a PR's own commit timestamps only mark when work was
    committed, not when it began.
    """
    if len(merged_at) < 2:
        return []
    ordered = sorted(merged_at)
    return [
        (ordered[i] - ordered[i - 1]).total_seconds() / 3600.0
        for i in range(1, len(ordered))
    ]


def iteration_stats(per_pr: list[int]) -> dict[str, float]:
    """Summarize iteration counts across PRs that reached LGTM.

    `per_pr` is the list of `iterations_before_lgtm` results (Nones already
    filtered out). `clean_merge_rate` is the share of PRs that landed LGTM with
    zero feedback rounds — a proxy for how often the loop nails it first try.
    """
    if not per_pr:
        return {
            "mean": 0.0,
            "median": 0.0,
            "max": 0.0,
            "clean_merge_rate": 0.0,
            "sample": 0.0,
        }
    floats = [float(n) for n in per_pr]
    clean = sum(1 for n in per_pr if n == 0)
    return {
        "mean": _mean(floats),
        "median": _median(floats),
        "max": float(max(per_pr)),
        "clean_merge_rate": clean / len(per_pr),
        "sample": float(len(per_pr)),
    }


def estimate_remaining(
    open_items: int, per_day: float, *, now: dt.datetime
) -> dict[str, object]:
    """Project how long the backlog will take at the current merge rate.

    Returns the remaining-item count, estimated days left, and an ETA date.
    When the rate is zero (no merges yet, or a fully stalled loop) the estimate
    is unknown rather than infinite.
    """
    if open_items <= 0:
        return {"open_items": 0, "days_remaining": 0.0, "eta": now, "known": True}
    if per_day <= 0:
        return {
            "open_items": open_items,
            "days_remaining": None,
            "eta": None,
            "known": False,
        }

    days_remaining = open_items / per_day
    eta = now + dt.timedelta(days=days_remaining)
    return {
        "open_items": open_items,
        "days_remaining": days_remaining,
        "eta": eta,
        "known": True,
    }


def churn_totals(churn: list[tuple[int, int, int]]) -> dict[str, int]:
    """Sum (additions, deletions, changed_files) tuples across merged PRs."""
    additions = sum(c[0] for c in churn)
    deletions = sum(c[1] for c in churn)
    files = sum(c[2] for c in churn)
    return {
        "additions": additions,
        "deletions": deletions,
        "net": additions - deletions,
        "files": files,
    }


def net_lines_from_code_frequency(weeks: list[list[int]]) -> int:
    """Sum GitHub's weekly code-frequency rows into the repo's net lines of code.

    Each row is `[unix_week, additions, deletions]` and GitHub reports deletions
    as a negative number, so the running net is just additions and deletions added
    straight across every week. Returns 0 for an empty history.
    """
    return sum(week[1] + week[2] for week in weeks)


def busiest_day(merged_at: list[dt.datetime]) -> tuple[str, int] | None:
    """Return the (ISO date, count) of the day with the most merges, or None."""
    if not merged_at:
        return None
    counter: Counter[str] = Counter(ts.date().isoformat() for ts in merged_at)
    day, count = counter.most_common(1)[0]
    return day, count
