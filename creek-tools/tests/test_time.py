"""Regression tests for ``creek.time`` and the BUG-002 timezone sweep.

These tests pin two facts that earlier code regressed on:

1. The :func:`creek.time.now_la` helper returns a tz-aware datetime
   anchored to America/Los_Angeles.
2. Default fragment / thread / eddy / decision timestamps are tz-aware
   in LA, so naive↔aware comparisons downstream do not raise.
3. Host timezone does not change behaviour: setting ``TZ`` to UTC,
   Asia/Tokyo, and America/New_York produces the same answers.
"""

from __future__ import annotations

import time
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from creek.link.threads import ThreadDetector
from creek.models import (
    Decision,
    Eddy,
    Fragment,
    FragmentSource,
    SourcePlatform,
    Thread,
)
from creek.time import LA_TZ, now_la, today_la


def test_now_la_is_tz_aware_in_la() -> None:
    """``now_la()`` is tz-aware and anchored to America/Los_Angeles."""
    moment = now_la()
    assert moment.tzinfo is not None
    assert moment.utcoffset() == ZoneInfo("America/Los_Angeles").utcoffset(moment)


def test_today_la_uses_la_calendar_not_utc(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``today_la()`` reads the LA wall calendar, not the UTC one.

    Replaces an earlier circular assertion. We freeze ``datetime.now``
    inside ``creek.time`` to a moment that is **already the next day
    in UTC but still the previous day in LA**: 03:30 UTC on May 4
    = 20:30 PDT on May 3. ``today_la()`` must return ``2026-05-03``;
    a naive implementation that called ``date.today()`` (host-tz
    dependent) or ``datetime.now(tz=UTC).date()`` would return
    ``2026-05-04`` and fail.
    """
    import creek.time as creek_time

    utc_moment = datetime(2026, 5, 4, 3, 30, tzinfo=ZoneInfo("UTC"))

    class _FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz: object = None) -> datetime:
            # ``now_la`` must always pass the LA tz; if a future refactor
            # drops the argument or passes UTC, the assertion makes the
            # regression loud rather than silent.
            assert tz == ZoneInfo("America/Los_Angeles"), (
                f"now_la() must call now(tz=LA_TZ); got tz={tz!r}"
            )
            return utc_moment.astimezone(ZoneInfo("America/Los_Angeles"))

    monkeypatch.setattr(creek_time, "datetime", _FrozenDatetime)

    assert today_la().isoformat() == "2026-05-03"


def test_la_tz_constant_matches_zoneinfo() -> None:
    """The exported ``LA_TZ`` constant is the LA zoneinfo, not a tzname str."""
    sample = datetime(2026, 4, 1, tzinfo=LA_TZ)
    assert sample.utcoffset() == ZoneInfo("America/Los_Angeles").utcoffset(sample)


class TestFragmentDefaultTimestamps:
    """Default ``Fragment.created`` / ``Fragment.ingested`` are tz-aware (BUG-002)."""

    def test_fragment_created_is_tz_aware(self) -> None:
        """A default-constructed Fragment carries a tz-aware ``created``."""
        frag = Fragment(
            id="frag-test1234",
            title="t",
            source=FragmentSource(platform=SourcePlatform.OTHER),
        )
        assert frag.created.tzinfo is not None

    def test_fragment_ingested_is_tz_aware(self) -> None:
        """A default-constructed Fragment carries a tz-aware ``ingested``."""
        frag = Fragment(
            id="frag-test1234",
            title="t",
            source=FragmentSource(platform=SourcePlatform.OTHER),
        )
        assert frag.ingested.tzinfo is not None

    def test_fragment_default_timestamps_are_la(self) -> None:
        """Fragment defaults agree with the LA timezone."""
        frag = Fragment(
            id="frag-test1234",
            title="t",
            source=FragmentSource(platform=SourcePlatform.OTHER),
        )
        la_offset = ZoneInfo("America/Los_Angeles").utcoffset(frag.created)
        assert frag.created.utcoffset() == la_offset


class TestThreadEddyDecisionDefaults:
    """Date defaults route through ``today_la`` (BUG-002)."""

    def test_thread_first_seen_is_la_date(self) -> None:
        """A default-constructed Thread carries a date computed in LA."""
        thread = Thread(title="t")
        assert thread.first_seen == today_la()

    def test_eddy_formed_is_la_date(self) -> None:
        """A default-constructed Eddy carries a date computed in LA."""
        eddy = Eddy(title="e")
        assert eddy.formed == today_la()

    def test_decision_opened_is_la_date(self) -> None:
        """A default-constructed Decision carries a date computed in LA."""
        decision = Decision(title="d")
        assert decision.opened == today_la()


class TestThreadDetectorDefaultNow:
    """ThreadDetector ``_now`` default is tz-aware (BUG-002)."""

    def test_default_now_is_tz_aware(self) -> None:
        """Constructing without an explicit ``now`` yields a tz-aware moment."""
        detector = ThreadDetector()
        assert detector._now.tzinfo is not None  # pin internal state


@pytest.mark.parametrize("tz_env", ["UTC", "Asia/Tokyo", "America/New_York"])
def test_fragment_defaults_independent_of_host_tz(
    tz_env: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Setting host ``TZ`` does not change Fragment default behaviour.

    The acceptance criterion from BUG-002: re-running with different
    host timezones produces identical assertions.
    """
    monkeypatch.setenv("TZ", tz_env)
    if hasattr(time, "tzset"):
        time.tzset()
    frag = Fragment(
        id="frag-test1234",
        title="t",
        source=FragmentSource(platform=SourcePlatform.OTHER),
    )
    # Regardless of host TZ, the offset matches LA.
    la_offset = ZoneInfo("America/Los_Angeles").utcoffset(frag.created)
    assert frag.created.utcoffset() == la_offset
