"""Regression tests for ``creek.time`` and the BUG-002 timezone sweep.

These tests pin the facts that earlier code regressed on:

1. The :func:`creek.time.now_la` helper returns a tz-aware datetime
   anchored to America/Los_Angeles.
2. Default fragment / thread / eddy / decision timestamps are tz-aware
   in LA, so naive↔aware comparisons downstream do not raise.
3. Host timezone does not change behaviour: setting ``TZ`` to UTC,
   Asia/Tokyo, and America/New_York produces the same answers.
4. :func:`creek.time.effective_authored_at` codifies the FEAT-031
   precedence (``authored_at`` → ``ingested``) that every time-bucket
   surface routes through.
5. :class:`creek.models.Fragment` anchors a naive ``created`` /
   ``ingested`` / ``authored_at`` to America/Los_Angeles at validation
   time (issue #976), so offsetless YAML frontmatter cannot smuggle a
   naive datetime into a vault that also holds tz-aware ones.
6. The LA anchor has exactly one production definition — ``LA_TZ`` in
   :mod:`creek.time` (issue #1339). Two constants that merely *agree*
   today are two places to change tomorrow.
"""

from __future__ import annotations

import re
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Final
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
    ensure_aware,
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


_CREEK_TOOLS_ROOT: Final[Path] = Path(__file__).resolve().parents[1]
"""The ``creek-tools`` package root — the directory holding ``creek/``."""

_PRODUCTION_ROOTS: Final[tuple[Path, ...]] = (
    _CREEK_TOOLS_ROOT / "creek",
    _CREEK_TOOLS_ROOT / "creek_mcp",
)
"""Production source trees scanned for ``LA_TZ`` definitions (``tests/`` excluded).

Test modules legitimately mint their own local ``LA_TZ`` for fixtures; the
single-definition rule is about production code, where a second constant is a
second thing to change.
"""

_LA_TZ_ASSIGNMENT: Final[re.Pattern[str]] = re.compile(
    r"^\s*LA_TZ\s*(?::[^=\n]+)?=[^=]",
    re.MULTILINE,
)
"""Matches an assignment to ``LA_TZ`` (annotated or not), never a comparison.

Anchored to an assignment so that ``from creek.time import LA_TZ`` and prose
mentions in docstrings do not count as definitions.
"""


def _production_files_declaring_la_tz() -> list[str]:
    """Return the production files that assign ``LA_TZ``, as relative paths.

    Returns:
        Repo-relative POSIX paths (e.g. ``creek/time.py``), sorted, one per
        file containing at least one ``LA_TZ = ...`` assignment.
    """
    return [
        source.relative_to(_CREEK_TOOLS_ROOT).as_posix()
        for root in _PRODUCTION_ROOTS
        for source in sorted(root.rglob("*.py"))
        if _LA_TZ_ASSIGNMENT.search(source.read_text(encoding="utf-8"))
    ]


def test_la_tz_is_declared_exactly_once_in_production() -> None:
    """``LA_TZ`` is defined once, in ``creek/time.py``, and re-exported (#1339).

    ``creek.ingest.base`` used to mint its own ``ZoneInfo("America/Los_Angeles")``
    under the same name. The two constants happened to agree, which is precisely
    the failure mode: the LA anchor is an ontology mandate (§8.3), so it needs
    one definition to change, audit, and reason about — not two that agree until
    someone edits one of them.

    The source scan is the load-bearing assertion, and it has to be: an
    *identity* check (``creek.ingest.base.LA_TZ is creek.time.LA_TZ``) cannot
    prove single-definition, because :class:`~zoneinfo.ZoneInfo` caches its
    instances by key — two independently constructed
    ``ZoneInfo("America/Los_Angeles")`` objects are already the same object,
    so such a check passes just as happily against the duplicated constant.
    The second assertion therefore pins a different fact: the historical
    ``from creek.ingest.base import LA_TZ`` path must keep resolving, to the
    LA zone, so the five modules importing it from there do not break on
    consolidation.
    """
    declaring = _production_files_declaring_la_tz()
    assert declaring == ["creek/time.py"], (
        "LA_TZ must be defined exactly once, in creek/time.py; "
        f"found definitions in: {declaring}"
    )

    from creek.ingest.base import LA_TZ as REEXPORTED_LA_TZ

    sample = datetime(2026, 4, 1, tzinfo=REEXPORTED_LA_TZ)
    assert sample.utcoffset() == ZoneInfo("America/Los_Angeles").utcoffset(sample), (
        "creek.ingest.base.LA_TZ must stay importable and anchored to "
        "America/Los_Angeles; existing importers depend on that path"
    )


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
        """Fragment defaults agree with the LA timezone.

        Post-#976 this is the only test pinning the default path:
        ``default_factory=now_la`` values never reach
        ``Fragment._normalise_timestamp`` because Pydantic does not run
        field validators over defaults. The defaults therefore have to
        be LA-aware on their own, and a future change that moved the LA
        anchoring *into* the validator would leave them naive with
        nothing else to catch it.
        """
        frag = Fragment(
            id="frag-test1234",
            title="t",
            source=FragmentSource(platform=SourcePlatform.OTHER),
        )
        la = ZoneInfo("America/Los_Angeles")
        assert frag.created.utcoffset() == la.utcoffset(frag.created)
        assert frag.ingested.utcoffset() == la.utcoffset(frag.ingested)


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

        The enforcer is :class:`~creek.models.Fragment`'s field
        validator over ``created`` / ``ingested`` / ``authored_at``
        (issue #976), **not** Pydantic's datetime parsing: Pydantic
        happily accepts an offsetless value and stores it naive, which
        is exactly what PyYAML hands back for a frontmatter line like
        ``ingested: 2024-01-01 12:00:00``. So both branches are fed
        *naive* inputs here — the shape that actually reaches the model
        off disk — and both must come back anchored to LA. Asserting
        only ``tzinfo is not None`` off an already-aware input would be
        vacuous; the offset assertion pins where it lands.
        """
        authored = datetime(2024, 3, 15)
        ingested = datetime(2026, 5, 24)
        with_authored = self._frag(authored_at=authored, ingested=ingested)
        without_authored = self._frag(authored_at=None, ingested=ingested)
        from_authored = effective_authored_at(with_authored)
        from_ingested = effective_authored_at(without_authored)
        la = ZoneInfo("America/Los_Angeles")
        assert from_authored.tzinfo is not None
        assert from_ingested.tzinfo is not None
        assert from_authored.utcoffset() == la.utcoffset(from_authored)
        assert from_ingested.utcoffset() == la.utcoffset(from_ingested)


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


class TestEnsureAware:
    """``ensure_aware`` makes any datetime safe to compare (issue #938).

    The helper exists because comparing a naive datetime against a
    tz-aware one raises ``TypeError: can't compare offset-naive and
    offset-aware datetimes``. Its contract is deliberately asymmetric:

    * naive in → *attach* America/Los_Angeles, keeping the wall clock;
    * aware in → return untouched, zone and all.

    The second half matters as much as the first: a fragment whose
    ``authored_at`` came off a Sydney source must keep its Sydney
    rendering, exactly as ``effective_authored_at`` already promises.
    """

    def test_naive_input_gets_the_la_timezone(self) -> None:
        """A naive input comes back anchored to America/Los_Angeles."""
        result = ensure_aware(datetime(2026, 7, 28, 23, 0, 0, 123456))
        assert result.tzinfo is not None
        assert result.utcoffset() == ZoneInfo("America/Los_Angeles").utcoffset(result)

    def test_naive_input_keeps_its_wall_clock_fields(self) -> None:
        """The helper *attaches* a zone; it must not convert the wall clock.

        Converting instead of attaching would shift 23:00 on the 28th to
        16:00 (or 06:00 the next day, depending on direction) and silently
        move fragments across day boundaries — the very class of bug that
        issue #938 is about.
        """
        result = ensure_aware(datetime(2026, 7, 28, 23, 0, 0, 123456))
        assert (result.year, result.month, result.day) == (2026, 7, 28)
        assert (result.hour, result.minute, result.second) == (23, 0, 0)
        assert result.microsecond == 123456

    def test_already_aware_non_la_input_is_returned_unchanged(self) -> None:
        """A Sydney-anchored datetime survives with its zone intact.

        Mirrors ``TestEffectiveAuthoredAt.test_preserves_timezone_of_authored_at``:
        no silent normalisation to LA or UTC, because callers downstream
        render the source's local time.
        """
        sydney = ZoneInfo("Australia/Sydney")
        moment = datetime(2024, 3, 15, 8, 30, 45, 123456, tzinfo=sydney)
        result = ensure_aware(moment)
        assert result == moment
        assert result.tzinfo == sydney
        assert result.hour == 8
        assert result.microsecond == 123456

    def test_already_aware_utc_input_is_returned_unchanged(self) -> None:
        """A UTC-anchored datetime is passed straight through."""
        moment = datetime(2026, 5, 24, 6, 0, 0, 654321, tzinfo=UTC)
        result = ensure_aware(moment)
        assert result == moment
        assert result.tzinfo == UTC
        assert result.hour == 6
        assert result.microsecond == 654321

    def test_result_is_always_comparable_with_now_la(self) -> None:
        """The invariant that actually matters: comparisons never raise.

        Both operand orders are exercised because Python dispatches to the
        *left* operand's ``__lt__`` first — an implementation that only
        normalised one side would still explode half the time.
        """
        past = ensure_aware(datetime(2020, 1, 1, 12, 0))
        future = ensure_aware(datetime(2099, 1, 1, 12, 0))
        reference = now_la()
        assert past < reference
        assert future >= reference
        assert reference >= past
        assert reference < future


class TestFragmentTimestampNormalisation:
    """``Fragment`` anchors naive timestamps to LA at validation (issue #976).

    Vault frontmatter routinely carries offsetless timestamps
    (``ingested: 2024-01-01 12:00:00``), which PyYAML parses into a
    *naive* :class:`~datetime.datetime`. Before #976 the model stored
    that verbatim, so a vault mixing offsetless and offset-carrying
    timestamps raised ``TypeError: can't compare offset-naive and
    offset-aware datetimes`` at whichever consumer compared them first
    (:class:`~creek.generate.synchronicity.SynchronicityDetector` is the
    reproducer). :func:`creek.time.ensure_aware` was the repair valve
    but had no caller on the load path; the model is now the chokepoint,
    so no downstream surface has to remember to repair anything.

    The contract inherits ``ensure_aware``'s asymmetry: naive in →
    *attach* LA (wall clock preserved); aware in → untouched, zone and
    microseconds intact; ``None`` in → still ``None``.
    """

    def test_naive_ingested_is_anchored_to_la(self) -> None:
        """A naive ``ingested`` comes back anchored to America/Los_Angeles."""
        frag = Fragment(
            id="frag-tznorm0001",
            title="t",
            source=FragmentSource(platform=SourcePlatform.OTHER),
            ingested=datetime(2025, 4, 20, 10, 0, 0),
        )
        la = ZoneInfo("America/Los_Angeles")
        assert frag.ingested.tzinfo is not None
        assert frag.ingested.utcoffset() == la.utcoffset(frag.ingested)

    def test_naive_created_is_anchored_to_la(self) -> None:
        """A naive ``created`` comes back anchored to America/Los_Angeles."""
        frag = Fragment(
            id="frag-tznorm0002",
            title="t",
            source=FragmentSource(platform=SourcePlatform.OTHER),
            created=datetime(2025, 4, 20, 10, 0, 0),
        )
        la = ZoneInfo("America/Los_Angeles")
        assert frag.created.tzinfo is not None
        assert frag.created.utcoffset() == la.utcoffset(frag.created)

    def test_naive_authored_at_is_anchored_to_la(self) -> None:
        """A naive ``authored_at`` comes back anchored to America/Los_Angeles.

        ``authored_at`` needs its own case because it is the only one of
        the three that is ``datetime | None`` — a validator written for
        the two non-optional fields could easily be left off it, and it
        is the field :func:`effective_authored_at` prefers.
        """
        frag = Fragment(
            id="frag-tznorm0003",
            title="t",
            source=FragmentSource(platform=SourcePlatform.OTHER),
            authored_at=datetime(2025, 4, 20, 10, 0, 0),
        )
        la = ZoneInfo("America/Los_Angeles")
        assert frag.authored_at is not None
        assert frag.authored_at.tzinfo is not None
        assert frag.authored_at.utcoffset() == la.utcoffset(frag.authored_at)

    def test_naive_coercion_preserves_the_wall_clock(self) -> None:
        """The zone is *attached*, never converted — 23:00 stays 23:00.

        This is the assertion that catches an ``astimezone()``
        implementation. ``astimezone`` on a naive value interprets it in
        the host timezone and then shifts the clock, so 23:00 on the
        28th becomes 16:00 on the 28th (or 06:00 on the 29th, depending
        on the host) — silently moving fragments across a day boundary
        and corrupting every date-bucketed surface. Only the
        wall-clock-preserving ``replace(tzinfo=...)`` behaviour of
        :func:`creek.time.ensure_aware` satisfies both halves of this
        test.
        """
        frag = Fragment(
            id="frag-tznorm0004",
            title="t",
            source=FragmentSource(platform=SourcePlatform.OTHER),
            ingested=datetime(2026, 7, 28, 23, 0, 0, 123456),
        )
        ingested = frag.ingested
        la = ZoneInfo("America/Los_Angeles")
        assert ingested.utcoffset() == la.utcoffset(ingested)
        assert (ingested.year, ingested.month, ingested.day) == (2026, 7, 28)
        assert (ingested.hour, ingested.minute, ingested.second) == (23, 0, 0)
        assert ingested.microsecond == 123456

    def test_iso_string_without_offset_is_anchored_to_la(self) -> None:
        """An offsetless ISO-8601 *string* is anchored too — after, not before.

        Load-bearing: this is the only case that distinguishes a
        ``mode="after"`` validator from a ``mode="before"`` one. A
        before-validator runs on the raw ``str`` and never sees a
        :class:`~datetime.datetime` at all, so it would hand the string
        to Pydantic untouched and the parsed result would stay naive.
        Frontmatter quoted as ``ingested: "2025-04-20T10:00:00"`` (or
        any JSON round-trip of a fragment) arrives in exactly this shape.
        """
        frag = Fragment.model_validate(
            {
                "type": "fragment",
                "id": "frag-tznorm0005",
                "title": "t",
                "source": {"platform": "other"},
                "ingested": "2025-04-20T10:00:00",
            },
        )
        la = ZoneInfo("America/Los_Angeles")
        assert frag.ingested.tzinfo is not None
        assert frag.ingested.utcoffset() == la.utcoffset(frag.ingested)

    def test_naive_datetime_from_yaml_mapping_is_anchored_to_la(self) -> None:
        """A naive ``datetime`` inside a validated mapping is anchored.

        The PyYAML shape: ``creek.vault.reader.try_load_fragment`` calls
        ``Fragment.model_validate`` on the parsed frontmatter mapping,
        in which an unquoted ``ingested: 2025-04-20 10:00:00`` has
        already become a naive ``datetime`` object. Pinned separately
        from the keyword-constructor cases so a validator that somehow
        only fires on one entry point cannot pass.
        """
        frag = Fragment.model_validate(
            {
                "type": "fragment",
                "id": "frag-tznorm0006",
                "title": "t",
                "source": {"platform": "other"},
                "ingested": datetime(2025, 4, 20, 10, 0, 0),
            },
        )
        la = ZoneInfo("America/Los_Angeles")
        assert frag.ingested.tzinfo is not None
        assert frag.ingested.utcoffset() == la.utcoffset(frag.ingested)

    def test_aware_sydney_authored_at_is_untouched(self) -> None:
        """An already-aware ``authored_at`` keeps its own zone, not LA.

        The other half of the asymmetric contract: coercion must not
        become normalisation. A Substack post authored in Sydney keeps
        its Sydney rendering, matching the promise
        :func:`effective_authored_at` already makes to callers that
        display source-local time.
        """
        sydney = ZoneInfo("Australia/Sydney")
        authored = datetime(2024, 3, 15, 8, 30, 45, 123456, tzinfo=sydney)
        frag = Fragment(
            id="frag-tznorm0007",
            title="t",
            source=FragmentSource(platform=SourcePlatform.OTHER),
            authored_at=authored,
        )
        assert frag.authored_at is not None
        assert frag.authored_at == authored
        assert frag.authored_at.tzinfo == sydney
        assert frag.authored_at.hour == 8
        assert frag.authored_at.microsecond == 123456

    def test_aware_utc_ingested_is_untouched(self) -> None:
        """An already-aware ``ingested`` passes straight through in UTC.

        Covers the aware branch on a non-optional field (the Sydney case
        covers it on the optional one), so both sides of the validator's
        branch are exercised rather than only the naive path.
        """
        moment = datetime(2026, 5, 24, 6, 0, 0, 654321, tzinfo=UTC)
        frag = Fragment(
            id="frag-tznorm0008",
            title="t",
            source=FragmentSource(platform=SourcePlatform.OTHER),
            ingested=moment,
        )
        assert frag.ingested == moment
        assert frag.ingested.tzinfo == UTC
        assert frag.ingested.hour == 6
        assert frag.ingested.microsecond == 654321

    def test_authored_at_none_is_preserved(self) -> None:
        """``authored_at=None`` stays ``None`` — the validator never invents one.

        ``None`` is the honest answer when no source date is
        extractable. A validator that reached for ``now_la()`` on the
        ``None`` branch would fabricate an authored date for every
        fragment whose source has none, which
        :func:`effective_authored_at` would then prefer over the real
        ``ingested`` value — silently re-dating the whole vault.
        """
        frag = Fragment(
            id="frag-tznorm0009",
            title="t",
            source=FragmentSource(platform=SourcePlatform.OTHER),
            authored_at=None,
        )
        assert frag.authored_at is None


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
