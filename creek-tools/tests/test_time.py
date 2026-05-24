"""Regression tests for ``creek.time`` and the BUG-002 timezone sweep.

These tests pin two facts that earlier code regressed on:

1. The :func:`creek.time.now_la` helper returns a tz-aware datetime
   anchored to America/Los_Angeles.
2. Default fragment / thread / eddy / decision timestamps are tz-aware
   in LA, so naive↔aware comparisons downstream do not raise.
3. Host timezone does not change behaviour: setting ``TZ`` to UTC,
   Asia/Tokyo, and America/New_York produces the same answers.
4. :func:`creek.time.effective_authored_at` codifies the FEAT-031
   precedence (``authored_at`` → ``ingested``) that every time-bucket
   surface routes through.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
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
from creek.time import (
    LA_TZ,
    effective_authored_at,
    effective_authored_date,
    now_la,
    today_la,
)


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


class TestEffectiveAuthoredAt:
    """``effective_authored_at`` pins the FEAT-031 precedence contract."""

    def _frag(
        self,
        *,
        authored_at: datetime | None,
        ingested: datetime,
        created: datetime | None = None,
    ) -> Fragment:
        """Build a Fragment with the three time fields under test.

        ``created`` defaults to ``ingested`` so the legacy field is
        irrelevant to the precedence assertions — the helper must
        consult ``authored_at`` and ``ingested`` only.
        """
        return Fragment(
            id="frag-effective01",
            title="t",
            source=FragmentSource(platform=SourcePlatform.OTHER),
            authored_at=authored_at,
            ingested=ingested,
            created=created if created is not None else ingested,
        )

    def test_returns_authored_at_when_populated(self) -> None:
        """Step 1 of the chain: ``authored_at`` wins outright."""
        authored = datetime(2024, 3, 15, 8, 30, tzinfo=UTC)
        ingested = datetime(2026, 5, 24, tzinfo=UTC)
        frag = self._frag(authored_at=authored, ingested=ingested)
        assert effective_authored_at(frag) == authored

    def test_falls_back_to_ingested_when_authored_at_none(self) -> None:
        """Step 2 of the chain: ``ingested`` is the honest fallback."""
        ingested = datetime(2026, 5, 24, tzinfo=UTC)
        frag = self._frag(authored_at=None, ingested=ingested)
        assert effective_authored_at(frag) == ingested

    def test_does_not_consult_created(self) -> None:
        """``created`` is deliberately not in the fallback chain.

        Historically ``created`` conflated source-authored date and
        filesystem mtime — bucketing on it produced misleading
        time-window results. The helper must skip it entirely so a
        legacy fragment with ``created`` set to filesystem mtime and
        ``ingested`` set to vault-write time anchors to the latter,
        not the former.
        """
        ingested = datetime(2026, 5, 24, tzinfo=UTC)
        created_filesystem_mtime = datetime(2024, 1, 1, tzinfo=UTC)
        frag = self._frag(
            authored_at=None,
            ingested=ingested,
            created=created_filesystem_mtime,
        )
        assert effective_authored_at(frag) == ingested
        assert effective_authored_at(frag) != frag.created

    def test_preserves_timezone_of_authored_at(self) -> None:
        """``authored_at`` is returned as-is — tz, microseconds, everything.

        The helper must not silently convert to UTC or LA: callers
        downstream may render the local-time component of the source,
        and a coercion here would erase that information.
        """
        sydney = ZoneInfo("Australia/Sydney")
        authored = datetime(2024, 3, 15, 8, 30, 45, 123456, tzinfo=sydney)
        ingested = datetime(2026, 5, 24, tzinfo=UTC)
        frag = self._frag(authored_at=authored, ingested=ingested)
        result = effective_authored_at(frag)
        assert result == authored
        assert result.tzinfo == sydney
        assert result.microsecond == 123456

    def test_result_is_always_tz_aware(self) -> None:
        """Neither branch produces a naive datetime — invariant for downstream.

        Fragment validates that ``ingested`` and ``authored_at`` (when
        present) are tz-aware via Pydantic's datetime parsing for both
        defaults and round-tripped YAML values, so this helper inherits
        the invariant. Pin it here so a future change to the model can't
        silently break temporal-linker comparisons.
        """
        authored = datetime(2024, 3, 15, tzinfo=UTC)
        ingested = datetime(2026, 5, 24, tzinfo=UTC)
        with_authored = self._frag(authored_at=authored, ingested=ingested)
        without_authored = self._frag(authored_at=None, ingested=ingested)
        assert effective_authored_at(with_authored).tzinfo is not None
        assert effective_authored_at(without_authored).tzinfo is not None


class TestEffectiveAuthoredDate:
    """``effective_authored_date`` is the date projection of ``authored_at``."""

    def test_returns_authored_date_when_authored_at_present(self) -> None:
        """Date component of ``authored_at`` wins."""
        authored = datetime(2024, 3, 15, 23, 45, tzinfo=UTC)
        ingested = datetime(2026, 5, 24, tzinfo=UTC)
        frag = Fragment(
            id="frag-date00001",
            title="t",
            source=FragmentSource(platform=SourcePlatform.OTHER),
            authored_at=authored,
            ingested=ingested,
        )
        assert effective_authored_date(frag) == authored.date()

    def test_falls_back_to_ingested_date(self) -> None:
        """Date component of ``ingested`` is the fallback."""
        ingested = datetime(2026, 5, 24, tzinfo=UTC)
        frag = Fragment(
            id="frag-date00002",
            title="t",
            source=FragmentSource(platform=SourcePlatform.OTHER),
            ingested=ingested,
        )
        assert effective_authored_date(frag) == ingested.date()


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
