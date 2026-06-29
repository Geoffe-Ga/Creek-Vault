"""Tests for the Ralph recap: pure stats helpers and the backlog count.

Run from the repo root with:

    python -m pytest scripts/ralph -q

`stats.py` is pure and tested directly. `recap.py` is the I/O shell; only its
adepthood-specific backlog filtering is unit-tested here (network calls are
monkeypatched), since everything else is a thin wrapper over `stats`.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest

import recap
import stats as rs

UTC = dt.timezone.utc


def _at(day: int, hour: int = 12) -> dt.datetime:
    return dt.datetime(2026, 6, day, hour, tzinfo=UTC)


# ---------- parse_iso ----------


def test_parse_iso_handles_trailing_z() -> None:
    parsed = rs.parse_iso("2026-06-27T08:30:00Z")
    assert parsed == dt.datetime(2026, 6, 27, 8, 30, tzinfo=UTC)


# ---------- normalize_verdict ----------


def test_normalize_verdict_returns_none_without_verdict_line() -> None:
    assert rs.normalize_verdict("Looks great, merging!") is None


def test_normalize_verdict_detects_lgtm() -> None:
    assert rs.normalize_verdict("Nice work.\nVerdict: LGTM") == rs.LGTM


def test_normalize_verdict_changes_requested_beats_lgtm_mention() -> None:
    body = "This is not yet LGTM.\nVerdict: CHANGES_REQUESTED"
    assert rs.normalize_verdict(body) == rs.CHANGES_REQUESTED


def test_normalize_verdict_defaults_to_comments() -> None:
    assert rs.normalize_verdict("Some notes.\nVerdict: COMMENTS") == rs.COMMENTS


# ---------- iterations_before_lgtm ----------


def test_iterations_before_lgtm_counts_rounds() -> None:
    verdicts = [rs.CHANGES_REQUESTED, rs.COMMENTS, rs.LGTM]
    assert rs.iterations_before_lgtm(verdicts) == 2


def test_iterations_before_lgtm_zero_for_clean_merge() -> None:
    assert rs.iterations_before_lgtm([rs.LGTM]) == 0


def test_iterations_before_lgtm_none_when_never_lgtm() -> None:
    assert rs.iterations_before_lgtm([rs.CHANGES_REQUESTED, rs.COMMENTS]) is None


# ---------- merge_rate ----------


def test_merge_rate_empty() -> None:
    rate = rs.merge_rate([], now=_at(27))
    assert rate == {"last_24h": 0.0, "per_hour": 0.0, "last_7_days": 0.0, "per_day": 0.0}


def test_merge_rate_last_24h_per_hour() -> None:
    # Two merges within 24h of `now` (27th 06:00 and 12:00); one is older.
    merged = [_at(26, 5), _at(27, 6), _at(27, 12)]
    rate = rs.merge_rate(merged, now=_at(27, 12))
    assert rate["last_24h"] == 2.0
    assert rate["per_hour"] == 2.0 / 24.0


def test_merge_rate_last_7_days_per_day() -> None:
    # Two merges within 7 days of the 28th; the 1st is outside the window.
    merged = [_at(1), _at(25), _at(27)]
    rate = rs.merge_rate(merged, now=_at(28))
    assert rate["last_7_days"] == 2.0
    assert rate["per_day"] == 2.0 / 7.0


def test_merge_rate_drops_stale_all_time_keys() -> None:
    rate = rs.merge_rate([_at(27)], now=_at(27))
    assert "total" not in rate
    assert "span_days" not in rate


# ---------- time_to_merge_stats ----------


def test_time_to_merge_stats() -> None:
    out = rs.time_to_merge_stats([1.0, 3.0, 5.0])
    assert out["median"] == 3.0
    assert out["fastest"] == 1.0
    assert out["slowest"] == 5.0
    assert out["mean"] == 3.0


# ---------- merge_intervals_hours ----------


def test_merge_intervals_hours_returns_consecutive_gaps() -> None:
    # 09:00, 12:00, 15:00 -> two 3-hour gaps.
    assert rs.merge_intervals_hours([_at(27, 9), _at(27, 12), _at(27, 15)]) == [3.0, 3.0]


def test_merge_intervals_hours_sorts_before_diffing() -> None:
    # Newest-first input (as the recap holds it) still yields positive gaps.
    assert rs.merge_intervals_hours([_at(27, 15), _at(27, 9), _at(27, 12)]) == [3.0, 3.0]


def test_merge_intervals_hours_empty_below_two_merges() -> None:
    assert rs.merge_intervals_hours([]) == []
    assert rs.merge_intervals_hours([_at(27, 9)]) == []


# ---------- iteration_stats ----------


def test_iteration_stats_clean_merge_rate() -> None:
    out = rs.iteration_stats([0, 0, 2, 4])
    assert out["clean_merge_rate"] == 0.5
    assert out["max"] == 4.0
    assert out["sample"] == 4.0


def test_iteration_stats_empty() -> None:
    out = rs.iteration_stats([])
    assert out["sample"] == 0.0


# ---------- estimate_remaining ----------


def test_estimate_remaining_projects_eta() -> None:
    est = rs.estimate_remaining(10, 2.0, now=_at(1))
    assert est["known"] is True
    assert est["days_remaining"] == 5.0
    assert est["eta"] == _at(6)


def test_estimate_remaining_unknown_when_rate_zero() -> None:
    est = rs.estimate_remaining(10, 0.0, now=_at(1))
    assert est["known"] is False
    assert est["days_remaining"] is None


def test_estimate_remaining_clear_backlog() -> None:
    est = rs.estimate_remaining(0, 2.0, now=_at(1))
    assert est["open_items"] == 0
    assert est["days_remaining"] == 0.0


# ---------- churn_totals ----------


def test_churn_totals_sums_and_nets() -> None:
    out = rs.churn_totals([(10, 3, 2), (5, 5, 1)])
    assert out["additions"] == 15
    assert out["deletions"] == 8
    assert out["net"] == 7
    assert out["files"] == 3


# ---------- net_lines_from_code_frequency ----------


def test_net_lines_from_code_frequency_sums_signed_weeks() -> None:
    # GitHub reports deletions as negatives: (100-30) + (50-10) = 110.
    weeks = [[1_700_000_000, 100, -30], [1_700_600_000, 50, -10]]
    assert rs.net_lines_from_code_frequency(weeks) == 110


def test_net_lines_from_code_frequency_empty_is_zero() -> None:
    assert rs.net_lines_from_code_frequency([]) == 0


# ---------- busiest_day ----------


def test_busiest_day_picks_max() -> None:
    merged = [_at(20), _at(20), _at(21)]
    result = rs.busiest_day(merged)
    assert result == ("2026-06-20", 2)


def test_busiest_day_none_when_empty() -> None:
    assert rs.busiest_day([]) is None


# ---------- count_open_backlog (adepthood adaptation) ----------


def _issue(number: int, *, labels: list[str], is_pr: bool = False) -> dict[str, Any]:
    issue: dict[str, Any] = {"number": number, "labels": [{"name": name} for name in labels]}
    if is_pr:
        issue["pull_request"] = {"url": f"https://example/{number}"}
    return issue


def _patch_issues(monkeypatch: pytest.MonkeyPatch, issues: list[dict[str, Any]]) -> None:
    monkeypatch.setattr(recap, "_gh_get_paged", lambda *a, **k: issues)


def test_count_open_backlog_excludes_prs_and_labelled_issues(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("RALPH_EXCLUDE_LABELS", raising=False)
    issues = [
        _issue(1, labels=[]),  # counted
        _issue(2, labels=["enhancement"]),  # counted
        _issue(3, labels=["epic"]),  # excluded by label
        _issue(4, labels=["blocked", "enhancement"]),  # excluded by label
        _issue(5, labels=[], is_pr=True),  # excluded as a PR
    ]
    _patch_issues(monkeypatch, issues)
    assert recap.count_open_backlog("owner/repo", token="t") == 2


def test_count_open_backlog_respects_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RALPH_EXCLUDE_LABELS", "deferred")
    issues = [
        _issue(1, labels=["epic"]),  # no longer excluded (override drops "epic")
        _issue(2, labels=["deferred"]),  # excluded by override
        _issue(3, labels=[]),  # counted
    ]
    _patch_issues(monkeypatch, issues)
    assert recap.count_open_backlog("owner/repo", token="t") == 2


# ---------- count_merged_total ----------


def test_count_merged_total_reads_search_total_count(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(recap, "_request_json", lambda *a, **k: {"total_count": 723, "items": []})
    assert recap.count_merged_total("owner/repo", token="t") == 723


# ---------- fetch_recent_merged_prs ----------


def _hit(number: int, *, merged: str, created: str) -> dict[str, Any]:
    return {"number": number, "created_at": created, "pull_request": {"merged_at": merged}}


def test_fetch_recent_merged_prs_sorts_newest_merge_first(monkeypatch: pytest.MonkeyPatch) -> None:
    hits = [
        _hit(1, merged="2026-06-25T00:00:00Z", created="2026-06-24T00:00:00Z"),
        _hit(2, merged="2026-06-27T00:00:00Z", created="2026-06-26T00:00:00Z"),
        _hit(3, merged="2026-06-26T00:00:00Z", created="2026-06-25T00:00:00Z"),
    ]
    monkeypatch.setattr(recap, "_gh_search_issues", lambda *a, **k: hits)
    out = recap.fetch_recent_merged_prs("owner/repo", token="t", since=_at(20).date(), max_prs=200)
    assert [pr["number"] for pr in out] == [2, 3, 1]


# ---------- _open_to_merge_hours ----------


def test_open_to_merge_hours_measures_open_to_merge_window() -> None:
    pr = _hit(1, merged="2026-06-27T12:00:00Z", created="2026-06-27T10:00:00Z")
    assert recap._open_to_merge_hours(pr) == 2.0


def test_open_to_merge_hours_clamps_negative_to_zero() -> None:
    # Clock skew (merge stamped before open) must not produce a negative window.
    pr = _hit(1, merged="2026-06-27T10:00:00Z", created="2026-06-27T12:00:00Z")
    assert recap._open_to_merge_hours(pr) == 0.0


# ---------- _pr_churn ----------


def test_pr_churn_reads_detail_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(recap, "fetch_pr_detail", lambda *a, **k: {"additions": 12, "deletions": 4, "changed_files": 3})
    assert recap._pr_churn("owner/repo", 1, token="t") == (12, 4, 3)


def test_pr_churn_degrades_to_zero_on_http_error(monkeypatch: pytest.MonkeyPatch) -> None:
    import urllib.error

    def _boom(*_a: Any, **_k: Any) -> dict[str, Any]:
        raise urllib.error.URLError("network down")

    monkeypatch.setattr(recap, "fetch_pr_detail", _boom)
    assert recap._pr_churn("owner/repo", 1, token="t") == (0, 0, 0)


# ---------- fetch_repo_net_lines ----------


def test_fetch_repo_net_lines_sums_code_frequency(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(recap, "_request_json", lambda *a, **k: [[1, 100, -40], [2, 20, -5]])
    assert recap.fetch_repo_net_lines("owner/repo", token="t") == 75


def test_fetch_repo_net_lines_none_when_stats_still_warming(monkeypatch: pytest.MonkeyPatch) -> None:
    # A 202 from GitHub yields an empty body (None); retries exhaust to None.
    monkeypatch.setattr(recap, "_request_json", lambda *a, **k: None)
    assert recap.fetch_repo_net_lines("owner/repo", token="t", attempts=2) is None


def test_fetch_repo_net_lines_none_on_http_error(monkeypatch: pytest.MonkeyPatch) -> None:
    import urllib.error

    def _boom(*_a: Any, **_k: Any) -> object:
        raise urllib.error.HTTPError("u", 500, "err", {}, None)  # type: ignore[arg-type]

    monkeypatch.setattr(recap, "_request_json", _boom)
    assert recap.fetch_repo_net_lines("owner/repo", token="t") is None


# ---------- this-PR display lines ----------


def test_this_pr_iter_line_variants() -> None:
    assert recap._this_pr_iter_line(None) == "merged without an LGTM verdict"
    assert recap._this_pr_iter_line(0) == "**0** rounds · clean first try"
    assert recap._this_pr_iter_line(1) == "**1** round to LGTM"
    assert recap._this_pr_iter_line(3) == "**3** rounds to LGTM"


def test_this_pr_tick_line_handles_first_merge() -> None:
    assert recap._this_pr_tick_line(None) == "first tracked merge"
    assert recap._this_pr_tick_line(0.5) == "**30m** since the previous merge"


def test_this_pr_review_line_formats_window() -> None:
    assert recap._this_pr_review_line(2.0) == "**2.0h** open → merge"


def test_loc_line_renders_three_windows() -> None:
    loc_24h = {"additions": 1200, "deletions": 300, "net": 900, "files": 5}
    loc_7d = {"additions": 8400, "deletions": 1600, "net": 6800, "files": 40}
    line = recap._loc_line(loc_24h, loc_7d, 124_500)
    assert line == "+1,200 / -300 (24h) · +8,400 / -1,600 (7d) · 124,500 net (full repo)"


def test_loc_line_placeholder_when_repo_net_unavailable() -> None:
    totals = {"additions": 0, "deletions": 0, "net": 0, "files": 0}
    line = recap._loc_line(totals, totals, None)
    assert line.endswith("— (full repo)")


# ---------- _heuristic_headline ----------


def test_heuristic_headline_strips_conventional_prefix() -> None:
    assert recap._heuristic_headline("feat(backend): add the energy ledger") == "add the energy ledger"


def test_heuristic_headline_clips_to_ten_words() -> None:
    headline = recap._heuristic_headline("one two three four five six seven eight nine ten eleven")
    assert headline == "one two three four five six seven eight nine ten"


def test_heuristic_headline_blank_title_falls_back() -> None:
    assert recap._heuristic_headline("   ") == "Latest change merged into the tick loop"


# ---------- generate_headline ----------


class _Block:
    def __init__(self, text: str, kind: str = "text") -> None:
        self.text = text
        self.type = kind


class _Response:
    def __init__(self, blocks: list[_Block]) -> None:
        self.content = blocks


class _FakeAnthropic:
    """Minimal stand-in for the anthropic SDK that records create() kwargs."""

    last_kwargs: dict[str, Any] = {}

    class _Client:
        def __init__(self) -> None:
            self.messages = _FakeAnthropic._Messages()

    class _Messages:
        def create(self, **kwargs: Any) -> _Response:
            _FakeAnthropic.last_kwargs = kwargs
            return _Response([_Block("Energy ledger now powers daily streaks")])

    def Anthropic(self) -> "_FakeAnthropic._Client":  # noqa: N802 - mirrors the SDK's class name
        return _FakeAnthropic._Client()


def test_generate_headline_uses_sdk_and_passes_low_effort(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeAnthropic()
    _FakeAnthropic.last_kwargs = {}
    monkeypatch.setattr(recap, "_anthropic_mod", fake)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")  # pragma: allowlist secret

    headline = recap.generate_headline("feat: add energy ledger", "Body text")

    assert headline == "Energy ledger now powers daily streaks"
    assert _FakeAnthropic.last_kwargs["model"] == recap.HEADLINE_MODEL
    assert _FakeAnthropic.last_kwargs["output_config"] == {"effort": "low"}


def test_generate_headline_falls_back_when_no_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(recap, "_anthropic_mod", _FakeAnthropic())
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    assert recap.generate_headline("feat: add energy ledger", "Body") == "add energy ledger"


def test_generate_headline_falls_back_when_sdk_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(recap, "_anthropic_mod", None)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")  # pragma: allowlist secret

    assert recap.generate_headline("feat: add energy ledger", "Body") == "add energy ledger"


def test_generate_headline_falls_back_on_sdk_error(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Boom:
        def Anthropic(self) -> object:  # noqa: N802 - mirrors the SDK's class name
            raise RuntimeError("api down")

    monkeypatch.setattr(recap, "_anthropic_mod", _Boom())
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")  # pragma: allowlist secret

    assert recap.generate_headline("feat: add energy ledger", "Body") == "add energy ledger"
