"""Tests for creek.generate.synchronicity — synchronicity detection.

Tests cover the SynchronicityDetector class and its two public methods:
detect_synchronicities (filtering resonances against synchronicity criteria)
and create_synchronicity_note (writing reflection notes to
``10-Liminal/Synchronicities/``).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, cast
from zoneinfo import ZoneInfo

import frontmatter
import pytest

from creek.generate.synchronicity import (
    DEFAULT_MIN_TIME_GAP_DAYS,
    DEFAULT_SIMILARITY_THRESHOLD,
    SynchronicityDetector,
)
from creek.link.embeddings import Resonance
from creek.models import (
    Fragment,
    FragmentLevel,
    FragmentSource,
    SourcePlatform,
    Synchronicity,
)

if TYPE_CHECKING:
    from pathlib import Path

# ---- Fixtures ----


@pytest.fixture()
def vault_path(tmp_path: Path) -> Path:
    """Create a minimal vault structure with the Liminal folder."""
    (tmp_path / "10-Liminal" / "Synchronicities").mkdir(parents=True, exist_ok=True)
    return tmp_path


@pytest.fixture()
def detector() -> SynchronicityDetector:
    """Create a SynchronicityDetector instance with default thresholds."""
    return SynchronicityDetector()


def _make_fragment(
    *,
    frag_id: str,
    title: str,
    platform: SourcePlatform,
    created: datetime,
) -> Fragment:
    """Construct a test Fragment with the given identifying fields.

    Mirrors ``created`` into ``ingested`` so the FEAT-031
    ``effective_authored_at`` helper — which falls back to ``ingested``
    when ``authored_at`` is unset — sees the time the test cares about.
    Without the mirror, time-gap and chronological-pair assertions
    would collapse to construction-time wall-clock.
    """
    return Fragment(
        id=frag_id,
        title=title,
        source=FragmentSource(platform=platform),
        created=created,
        ingested=created,
    )


@pytest.fixture()
def cross_source_pair() -> dict[str, Fragment]:
    """Return two fragments on different platforms separated by >30 days."""
    return {
        "frag-a": _make_fragment(
            frag_id="frag-a",
            title="the river remembers every stone it has touched",
            platform=SourcePlatform.DISCORD,
            created=datetime(2025, 1, 5, 10, 0, 0),
        ),
        "frag-b": _make_fragment(
            frag_id="frag-b",
            title="the river remembers every stone it has touched",
            platform=SourcePlatform.JOURNAL,
            created=datetime(2025, 4, 20, 10, 0, 0),
        ),
    }


@pytest.fixture()
def same_source_pair() -> dict[str, Fragment]:
    """Return two fragments on the same platform."""
    return {
        "frag-a": _make_fragment(
            frag_id="frag-a",
            title="the river remembers every stone",
            platform=SourcePlatform.JOURNAL,
            created=datetime(2025, 1, 5, 10, 0, 0),
        ),
        "frag-b": _make_fragment(
            frag_id="frag-b",
            title="the river remembers every stone",
            platform=SourcePlatform.JOURNAL,
            created=datetime(2025, 4, 20, 10, 0, 0),
        ),
    }


@pytest.fixture()
def close_time_pair() -> dict[str, Fragment]:
    """Return cross-source fragments less than 30 days apart."""
    return {
        "frag-a": _make_fragment(
            frag_id="frag-a",
            title="forgiveness keeps arriving unannounced",
            platform=SourcePlatform.DISCORD,
            created=datetime(2025, 1, 5, 10, 0, 0),
        ),
        "frag-b": _make_fragment(
            frag_id="frag-b",
            title="forgiveness keeps arriving unannounced",
            platform=SourcePlatform.JOURNAL,
            created=datetime(2025, 1, 20, 10, 0, 0),
        ),
    }


# ---- Module Constants ----


class TestConstants:
    """Tests for module-level threshold constants."""

    def test_similarity_threshold_is_point_nine(self) -> None:
        """Threshold sits at 0.9; detector accepts strictly greater values."""
        assert pytest.approx(0.9) == DEFAULT_SIMILARITY_THRESHOLD

    def test_time_gap_default_is_30_days(self) -> None:
        """Default minimum gap between fragments is 30 days."""
        assert DEFAULT_MIN_TIME_GAP_DAYS == 30


# ---- Detector Init ----


class TestSynchronicityDetectorInit:
    """Tests for SynchronicityDetector.__init__."""

    def test_defaults(self) -> None:
        """Default constructor uses module-level thresholds."""
        det = SynchronicityDetector()
        assert det.similarity_threshold == DEFAULT_SIMILARITY_THRESHOLD
        assert det.min_time_gap_days == DEFAULT_MIN_TIME_GAP_DAYS

    def test_accepts_overrides(self) -> None:
        """Constructor should accept threshold overrides."""
        det = SynchronicityDetector(
            similarity_threshold=0.95,
            min_time_gap_days=60,
        )
        assert det.similarity_threshold == pytest.approx(0.95)
        assert det.min_time_gap_days == 60


# ---- detect_synchronicities ----


class TestDetectSynchronicities:
    """Tests for SynchronicityDetector.detect_synchronicities."""

    def test_empty_resonances_returns_empty(
        self,
        detector: SynchronicityDetector,
    ) -> None:
        """Empty resonance list yields no synchronicities."""
        assert detector.detect_synchronicities([], {}) == []

    def test_missing_fragment_lookup_skipped(
        self,
        detector: SynchronicityDetector,
    ) -> None:
        """Resonances referencing unknown fragment IDs are skipped."""
        resonances = [("frag-a", "frag-b", 0.95)]
        result = detector.detect_synchronicities(resonances, {})
        assert result == []

    def test_detects_valid_cross_source_pair(
        self,
        detector: SynchronicityDetector,
        cross_source_pair: dict[str, Fragment],
    ) -> None:
        """Cross-source, >30 day, >0.9 similarity pair is a synchronicity."""
        resonances = [("frag-a", "frag-b", 0.95)]
        result = detector.detect_synchronicities(resonances, cross_source_pair)
        assert len(result) == 1
        sync = result[0]
        assert isinstance(sync, Synchronicity)
        assert sync.similarity == pytest.approx(0.95)

    def test_records_time_gap(
        self,
        detector: SynchronicityDetector,
        cross_source_pair: dict[str, Fragment],
    ) -> None:
        """Detected synchronicity records absolute gap in days."""
        resonances = [("frag-a", "frag-b", 0.95)]
        result = detector.detect_synchronicities(resonances, cross_source_pair)
        # 2025-01-05 -> 2025-04-20 = 105 days
        assert result[0].time_gap_days == 105

    def test_time_gap_is_absolute(
        self,
        detector: SynchronicityDetector,
        cross_source_pair: dict[str, Fragment],
    ) -> None:
        """Time gap is always non-negative regardless of argument order."""
        resonances = [("frag-b", "frag-a", 0.95)]
        result = detector.detect_synchronicities(resonances, cross_source_pair)
        assert result[0].time_gap_days == 105

    def test_orders_fragments_chronologically(
        self,
        detector: SynchronicityDetector,
        cross_source_pair: dict[str, Fragment],
    ) -> None:
        """Synchronicity stores earlier fragment as ``fragment_a_id``."""
        resonances = [("frag-b", "frag-a", 0.95)]
        result = detector.detect_synchronicities(resonances, cross_source_pair)
        assert result[0].fragment_a_id == "frag-a"
        assert result[0].fragment_b_id == "frag-b"

    def test_filters_low_similarity(
        self,
        detector: SynchronicityDetector,
        cross_source_pair: dict[str, Fragment],
    ) -> None:
        """Resonances below 0.9 are filtered out."""
        resonances = [("frag-a", "frag-b", 0.85)]
        result = detector.detect_synchronicities(resonances, cross_source_pair)
        assert result == []

    def test_boundary_similarity_excluded(
        self,
        detector: SynchronicityDetector,
        cross_source_pair: dict[str, Fragment],
    ) -> None:
        """Exactly 0.9 does not qualify — threshold is strict > 0.9."""
        resonances = [("frag-a", "frag-b", 0.9)]
        result = detector.detect_synchronicities(resonances, cross_source_pair)
        assert result == []

    def test_filters_same_source(
        self,
        detector: SynchronicityDetector,
        same_source_pair: dict[str, Fragment],
    ) -> None:
        """Fragments from the same source platform are not synchronicities."""
        resonances = [("frag-a", "frag-b", 0.95)]
        result = detector.detect_synchronicities(resonances, same_source_pair)
        assert result == []

    def test_filters_close_in_time(
        self,
        detector: SynchronicityDetector,
        close_time_pair: dict[str, Fragment],
    ) -> None:
        """Fragments less than 30 days apart are filtered out."""
        resonances = [("frag-a", "frag-b", 0.95)]
        result = detector.detect_synchronicities(resonances, close_time_pair)
        assert result == []

    def test_filters_status_update_phrases(
        self,
        detector: SynchronicityDetector,
    ) -> None:
        """Fragments containing status-update phrases are filtered out."""
        fragments = {
            "frag-a": _make_fragment(
                frag_id="frag-a",
                title="still working on the migration plan",
                platform=SourcePlatform.DISCORD,
                created=datetime(2025, 1, 1, 10, 0, 0),
            ),
            "frag-b": _make_fragment(
                frag_id="frag-b",
                title="still working on the migration plan",
                platform=SourcePlatform.JOURNAL,
                created=datetime(2025, 4, 1, 10, 0, 0),
            ),
        }
        resonances = [("frag-a", "frag-b", 0.95)]
        assert detector.detect_synchronicities(resonances, fragments) == []

    def test_filters_progress_update_phrase(
        self,
        detector: SynchronicityDetector,
    ) -> None:
        """The ``progress on`` phrase also counts as status noise."""
        fragments = {
            "frag-a": _make_fragment(
                frag_id="frag-a",
                title="progress on the onboarding doc",
                platform=SourcePlatform.DISCORD,
                created=datetime(2025, 1, 1, 10, 0, 0),
            ),
            "frag-b": _make_fragment(
                frag_id="frag-b",
                title="progress on the onboarding doc",
                platform=SourcePlatform.JOURNAL,
                created=datetime(2025, 4, 1, 10, 0, 0),
            ),
        }
        resonances = [("frag-a", "frag-b", 0.95)]
        assert detector.detect_synchronicities(resonances, fragments) == []

    def test_filters_shared_project_proper_noun(
        self,
        detector: SynchronicityDetector,
    ) -> None:
        """Shared proper-noun project names filter out the pair."""
        fragments = {
            "frag-a": _make_fragment(
                frag_id="frag-a",
                title="ideas for Mycelium launch",
                platform=SourcePlatform.DISCORD,
                created=datetime(2025, 1, 1, 10, 0, 0),
            ),
            "frag-b": _make_fragment(
                frag_id="frag-b",
                title="more thoughts on Mycelium scope",
                platform=SourcePlatform.JOURNAL,
                created=datetime(2025, 4, 1, 10, 0, 0),
            ),
        }
        resonances = [("frag-a", "frag-b", 0.95)]
        assert detector.detect_synchronicities(resonances, fragments) == []

    def test_allows_shared_common_words(
        self,
        detector: SynchronicityDetector,
    ) -> None:
        """Shared common (non-proper-noun) words do not trigger the filter."""
        fragments = {
            "frag-a": _make_fragment(
                frag_id="frag-a",
                title="the light moves through the water slowly",
                platform=SourcePlatform.DISCORD,
                created=datetime(2025, 1, 1, 10, 0, 0),
            ),
            "frag-b": _make_fragment(
                frag_id="frag-b",
                title="the light moves through the water slowly",
                platform=SourcePlatform.JOURNAL,
                created=datetime(2025, 4, 1, 10, 0, 0),
            ),
        }
        resonances = [("frag-a", "frag-b", 0.95)]
        result = detector.detect_synchronicities(resonances, fragments)
        assert len(result) == 1

    def test_ignores_sentence_initial_capitals(
        self,
        detector: SynchronicityDetector,
    ) -> None:
        """A capital at sentence start is not treated as a proper-noun match."""
        fragments = {
            "frag-a": _make_fragment(
                frag_id="frag-a",
                title="Forgiveness keeps arriving",
                platform=SourcePlatform.DISCORD,
                created=datetime(2025, 1, 1, 10, 0, 0),
            ),
            "frag-b": _make_fragment(
                frag_id="frag-b",
                title="Forgiveness keeps arriving",
                platform=SourcePlatform.JOURNAL,
                created=datetime(2025, 4, 1, 10, 0, 0),
            ),
        }
        resonances = [("frag-a", "frag-b", 0.95)]
        result = detector.detect_synchronicities(resonances, fragments)
        assert len(result) == 1

    def test_multiple_resonances_detected_independently(
        self,
        detector: SynchronicityDetector,
    ) -> None:
        """Each qualifying resonance produces exactly one synchronicity."""
        fragments = {
            "frag-a": _make_fragment(
                frag_id="frag-a",
                title="the river holds everything",
                platform=SourcePlatform.DISCORD,
                created=datetime(2025, 1, 1, 10, 0, 0),
            ),
            "frag-b": _make_fragment(
                frag_id="frag-b",
                title="the river holds everything",
                platform=SourcePlatform.JOURNAL,
                created=datetime(2025, 4, 1, 10, 0, 0),
            ),
            "frag-c": _make_fragment(
                frag_id="frag-c",
                title="silence has a shape",
                platform=SourcePlatform.EMAIL,
                created=datetime(2025, 1, 2, 10, 0, 0),
            ),
            "frag-d": _make_fragment(
                frag_id="frag-d",
                title="silence has a shape",
                platform=SourcePlatform.ESSAY,
                created=datetime(2025, 5, 1, 10, 0, 0),
            ),
        }
        resonances = [
            ("frag-a", "frag-b", 0.95),
            ("frag-c", "frag-d", 0.96),
        ]
        result = detector.detect_synchronicities(resonances, fragments)
        assert len(result) == 2

    def test_sources_recorded_from_fragments(
        self,
        detector: SynchronicityDetector,
        cross_source_pair: dict[str, Fragment],
    ) -> None:
        """Synchronicity records the source platform of each fragment."""
        resonances = [("frag-a", "frag-b", 0.95)]
        result = detector.detect_synchronicities(resonances, cross_source_pair)
        sync = result[0]
        # Chronological order: frag-a (Discord) first, frag-b (Journal) second
        assert sync.source_a == SourcePlatform.DISCORD
        assert sync.source_b == SourcePlatform.JOURNAL


# ---- FEAT-031 effective_authored_at precedence ----


def _authored_fragment(
    *,
    frag_id: str,
    title: str,
    platform: SourcePlatform,
    ingest_moment: datetime,
    authored_at: datetime,
) -> Fragment:
    """Build a Fragment with split ``created`` vs ``authored_at`` timestamps.

    Models the real-world ingestion case FEAT-031 was filed for: a
    multi-year corpus ingested in one batch, where ``created`` /
    ``ingested`` collapse to a single wall-clock moment but each
    fragment's source-side authored date lives in its own past.
    """
    return Fragment(
        id=frag_id,
        title=title,
        source=FragmentSource(platform=platform),
        created=ingest_moment,
        ingested=ingest_moment,
        authored_at=authored_at,
    )


class TestEffectiveAuthoredAtPrecedence:
    """FEAT-031: gap / chronological ordering / rendered body use authored_at."""

    def test_time_gap_uses_authored_at_not_created(
        self,
        detector: SynchronicityDetector,
    ) -> None:
        """The synchronicity gap is computed from ``authored_at``.

        Both fragments share the same ``created`` (ingested in the same
        batch) but their ``authored_at`` values are 105 days apart on
        different platforms. Without FEAT-031, the gap would collapse
        to 0 and the synchronicity would be filtered out by the
        ``min_time_gap_days`` rule.
        """
        ingest_moment = datetime(2026, 5, 1, 10, 0, 0)
        fragments = {
            "frag-a": _authored_fragment(
                frag_id="frag-a",
                title="the river remembers every stone it has touched",
                platform=SourcePlatform.DISCORD,
                ingest_moment=ingest_moment,
                authored_at=datetime(2025, 1, 5, 10, 0, 0),
            ),
            "frag-b": _authored_fragment(
                frag_id="frag-b",
                title="the river remembers every stone it has touched",
                platform=SourcePlatform.JOURNAL,
                ingest_moment=ingest_moment,
                authored_at=datetime(2025, 4, 20, 10, 0, 0),
            ),
        }
        resonances = [("frag-a", "frag-b", 0.95)]
        result = detector.detect_synchronicities(resonances, fragments)
        assert len(result) == 1
        assert result[0].time_gap_days == 105

    def test_chronological_pair_uses_authored_at(
        self,
        detector: SynchronicityDetector,
    ) -> None:
        """Earlier-by-``authored_at`` becomes ``fragment_a_id``.

        ``frag-late-creation`` is ``created`` *first* (in the ingest
        order) but ``authored_at`` *later*. Chronological pairing must
        therefore put ``frag-early-creation`` first.
        """
        fragments = {
            "frag-late-creation": _authored_fragment(
                frag_id="frag-late-creation",
                title="the long quiet between voices",
                platform=SourcePlatform.DISCORD,
                ingest_moment=datetime(2026, 5, 1, 8, 0, 0),
                authored_at=datetime(2026, 1, 10, 9, 0, 0),
            ),
            "frag-early-creation": _authored_fragment(
                frag_id="frag-early-creation",
                title="the long quiet between voices",
                platform=SourcePlatform.JOURNAL,
                ingest_moment=datetime(2026, 5, 1, 9, 0, 0),
                authored_at=datetime(2024, 9, 1, 9, 0, 0),
            ),
        }
        resonances = [("frag-late-creation", "frag-early-creation", 0.95)]
        result = detector.detect_synchronicities(resonances, fragments)
        assert len(result) == 1
        # Earlier authored_at wins, regardless of ingest order.
        assert result[0].fragment_a_id == "frag-early-creation"
        assert result[0].fragment_b_id == "frag-late-creation"

    def test_render_body_uses_authored_at_date(
        self,
        detector: SynchronicityDetector,
        vault_path: Path,
    ) -> None:
        """The rendered note body shows authored dates, not ingest dates.

        The body's bulleted excerpts include the date for each fragment.
        FEAT-031 requires that date to be the authored date — the answer
        to "when was this written?" — not the wall-clock moment the
        vault wrote the fragment.
        """
        ingest_moment = datetime(2026, 5, 1, 10, 0, 0)
        fragments = {
            "frag-a": _authored_fragment(
                frag_id="frag-a",
                title="the river remembers every stone it has touched",
                platform=SourcePlatform.DISCORD,
                ingest_moment=ingest_moment,
                authored_at=datetime(2025, 1, 5, 10, 0, 0),
            ),
            "frag-b": _authored_fragment(
                frag_id="frag-b",
                title="the river remembers every stone it has touched",
                platform=SourcePlatform.JOURNAL,
                ingest_moment=ingest_moment,
                authored_at=datetime(2025, 4, 20, 10, 0, 0),
            ),
        }
        resonances = [("frag-a", "frag-b", 0.95)]
        syncs = detector.detect_synchronicities(resonances, fragments)
        note_path = detector.create_synchronicity_note(
            syncs[0],
            fragments,
            vault_path,
        )
        body = frontmatter.loads(note_path.read_text(encoding="utf-8")).content
        # Authored dates are present; ingest date is not.
        assert "2025-01-05" in body
        assert "2025-04-20" in body
        assert ingest_moment.date().isoformat() not in body


# ---- Issue #976: mixed naive / aware vaults ----


class TestMixedNaiveAndAwareTimestamps:
    """A vault mixing offsetless and offset-carrying timestamps still detects.

    This is the consumer-side regression for issue #976. The fixtures
    above feed naive datetimes on *both* sides of every pair, so they
    never compare a naive value against an aware one and the suite stayed
    green while the defect was live. A real vault is not so tidy: journal
    frontmatter written as ``ingested: 2024-01-01 09:00:00`` (naive, per
    PyYAML) sits next to a Substack fragment whose ``authored_at`` was
    extracted with a real offset. Detection then walked into
    ``TypeError: can't compare offset-naive and offset-aware datetimes``
    in :meth:`SynchronicityDetector._chronological_pair`, killing the
    whole ``creek report --type synchronicity`` run rather than one pair.

    ``detect_synchronicities`` is the cheapest public entry point that
    reproduces it: it accepts the legacy ``(id_a, id_b, similarity)``
    tuple, so two fragments and one tuple exercise the crash with no
    embeddings, no LLM, and no I/O.
    """

    def test_mixed_naive_and_aware_vault_yields_synchronicity(
        self,
        detector: SynchronicityDetector,
    ) -> None:
        """A naive-``ingested`` fragment pairs with an aware-``authored_at`` one.

        Asserts the *result*, not merely that nothing raised: a
        "does not raise" test would be exactly the vacuousness issue
        #976 is about. The expected gap is computed from the coerced
        instants — 2024-01-01 09:00 anchored to LA (17:00Z) against
        2024-06-01 09:00 in Sydney (2024-05-31 23:00Z) — which is 151
        days and 6 hours, so ``time_gap_days`` is 151.

        The gap alone does *not* separate the correct
        ``replace(tzinfo=LA_TZ)`` coercion from an ``astimezone``-based
        one: the ``astimezone`` variant only shifts this gap to 152 on
        hosts at UTC+12 or further east (Pacific/Auckland,
        Pacific/Kiritimati), so it survives here on every realistic dev
        or CI host. That variant is killed instead by
        ``test_naive_coercion_preserves_the_wall_clock`` in
        ``tests/test_time.py``, which pins the wall-clock fields rather
        than an elapsed gap.
        """
        sydney = ZoneInfo("Australia/Sydney")
        naive_journal = _make_fragment(
            frag_id="frag-naive-ingested",
            title="the kettle boils in an empty kitchen",
            platform=SourcePlatform.JOURNAL,
            created=datetime(2024, 1, 1, 9, 0, 0),
        )
        aware_substack = _authored_fragment(
            frag_id="frag-aware-authored",
            title="a bell rung twice at the edge of sleep",
            platform=SourcePlatform.SUBSTACK,
            ingest_moment=datetime(2024, 6, 2, 12, 0, 0, tzinfo=UTC),
            authored_at=datetime(2024, 6, 1, 9, 0, 0, tzinfo=sydney),
        )
        fragments = {
            naive_journal.id: naive_journal,
            aware_substack.id: aware_substack,
        }
        resonances = [("frag-naive-ingested", "frag-aware-authored", 0.95)]

        result = detector.detect_synchronicities(resonances, fragments)

        assert len(result) == 1
        # The naive-timestamped fragment is the earlier of the two once
        # both sides are comparable, so it lands in ``fragment_a_id``.
        assert result[0].fragment_a_id == "frag-naive-ingested"
        assert result[0].fragment_b_id == "frag-aware-authored"
        assert result[0].time_gap_days == 151


# ---- FEAT-024 cross-level ranking ----


def _level_fragment(
    *,
    frag_id: str,
    title: str,
    platform: SourcePlatform,
    created: datetime,
    level: FragmentLevel,
) -> Fragment:
    """Construct a test Fragment with an explicit structural level.

    Mirrors ``created`` into ``ingested`` so the FEAT-031
    ``effective_authored_at`` helper resolves to the supplied time
    when ``authored_at`` is unset.
    """
    return Fragment(
        id=frag_id,
        title=title,
        source=FragmentSource(platform=platform),
        created=created,
        ingested=created,
        level=level,
    )


class TestCrossLevelRanking:
    """Tests for FEAT-024 cross-level synchronicity ranking."""

    def test_accepts_resonance_objects(
        self,
        detector: SynchronicityDetector,
        cross_source_pair: dict[str, Fragment],
    ) -> None:
        """Detector accepts the new Resonance record (FEAT-024)."""
        resonances = [
            Resonance(
                fragment_a_id="frag-a",
                fragment_b_id="frag-b",
                similarity=0.95,
                from_level="sentence",
                to_level="document",
            ),
        ]
        result = detector.detect_synchronicities(resonances, cross_source_pair)
        assert len(result) == 1
        # Chronological order keeps frag-a first, so its level is level_a.
        assert result[0].level_a == "sentence"
        assert result[0].level_b == "document"

    def test_tuple_input_defaults_to_document_levels(
        self,
        detector: SynchronicityDetector,
        cross_source_pair: dict[str, Fragment],
    ) -> None:
        """Legacy 3-tuple input keeps the default 'document' levels."""
        resonances = [("frag-a", "frag-b", 0.95)]
        result = detector.detect_synchronicities(resonances, cross_source_pair)
        assert len(result) == 1
        assert result[0].level_a == "document"
        assert result[0].level_b == "document"

    def test_cross_level_ranks_before_same_level(
        self,
        detector: SynchronicityDetector,
    ) -> None:
        """Cross-level pairs surface ahead of same-level pairs."""
        fragments = {
            "same-a": _level_fragment(
                frag_id="same-a",
                title="the wind through the long grass",
                platform=SourcePlatform.DISCORD,
                created=datetime(2025, 1, 1, 10, 0, 0),
                level="document",
            ),
            "same-b": _level_fragment(
                frag_id="same-b",
                title="the wind through the long grass",
                platform=SourcePlatform.JOURNAL,
                created=datetime(2025, 4, 1, 10, 0, 0),
                level="document",
            ),
            "cross-a": _level_fragment(
                frag_id="cross-a",
                title="silence has a shape",
                platform=SourcePlatform.EMAIL,
                created=datetime(2025, 1, 2, 10, 0, 0),
                level="sentence",
            ),
            "cross-b": _level_fragment(
                frag_id="cross-b",
                title="silence has a shape",
                platform=SourcePlatform.ESSAY,
                created=datetime(2025, 5, 1, 10, 0, 0),
                level="exchange",
            ),
        }
        # Give the same-level pair a *higher* raw similarity to prove the
        # cross-level boost overrides naive similarity ordering.
        resonances = [
            Resonance(
                fragment_a_id="same-a",
                fragment_b_id="same-b",
                similarity=0.99,
                from_level="document",
                to_level="document",
            ),
            Resonance(
                fragment_a_id="cross-a",
                fragment_b_id="cross-b",
                similarity=0.92,
                from_level="sentence",
                to_level="exchange",
            ),
        ]
        result = detector.detect_synchronicities(resonances, fragments)
        assert len(result) == 2
        # Cross-level should come first.
        assert result[0].fragment_a_id == "cross-a"
        assert result[1].fragment_a_id == "same-a"

    def test_within_group_sorted_by_similarity_descending(
        self,
        detector: SynchronicityDetector,
    ) -> None:
        """Two cross-level pairs are ranked by similarity descending."""
        fragments = {
            "x1": _level_fragment(
                frag_id="x1",
                title="cedar smoke at dusk",
                platform=SourcePlatform.DISCORD,
                created=datetime(2025, 1, 1, 10, 0, 0),
                level="sentence",
            ),
            "x2": _level_fragment(
                frag_id="x2",
                title="cedar smoke at dusk",
                platform=SourcePlatform.JOURNAL,
                created=datetime(2025, 4, 1, 10, 0, 0),
                level="exchange",
            ),
            "y1": _level_fragment(
                frag_id="y1",
                title="folding the laundry slowly",
                platform=SourcePlatform.EMAIL,
                created=datetime(2025, 1, 2, 10, 0, 0),
                level="paragraph",
            ),
            "y2": _level_fragment(
                frag_id="y2",
                title="folding the laundry slowly",
                platform=SourcePlatform.ESSAY,
                created=datetime(2025, 5, 1, 10, 0, 0),
                level="session",
            ),
        }
        resonances = [
            Resonance(
                fragment_a_id="x1",
                fragment_b_id="x2",
                similarity=0.91,
                from_level="sentence",
                to_level="exchange",
            ),
            Resonance(
                fragment_a_id="y1",
                fragment_b_id="y2",
                similarity=0.97,
                from_level="paragraph",
                to_level="session",
            ),
        ]
        result = detector.detect_synchronicities(resonances, fragments)
        assert len(result) == 2
        # Both are cross-level — higher similarity wins.
        assert result[0].fragment_a_id == "y1"
        assert result[1].fragment_a_id == "x1"

    def test_level_swaps_with_chronological_order(
        self,
        detector: SynchronicityDetector,
    ) -> None:
        """When the later fragment is listed first, its level follows."""
        fragments = {
            "early": _level_fragment(
                frag_id="early",
                title="moss on the north side of the stone",
                platform=SourcePlatform.DISCORD,
                created=datetime(2025, 1, 1, 10, 0, 0),
                level="paragraph",
            ),
            "late": _level_fragment(
                frag_id="late",
                title="moss on the north side of the stone",
                platform=SourcePlatform.JOURNAL,
                created=datetime(2025, 5, 1, 10, 0, 0),
                level="sentence",
            ),
        }
        # Resonance lists later -> earlier; level_a / level_b on the
        # synchronicity must still reflect the chronological order.
        resonances = [
            Resonance(
                fragment_a_id="late",
                fragment_b_id="early",
                similarity=0.95,
                from_level="sentence",
                to_level="paragraph",
            ),
        ]
        result = detector.detect_synchronicities(resonances, fragments)
        assert len(result) == 1
        sync = result[0]
        assert sync.fragment_a_id == "early"
        assert sync.fragment_b_id == "late"
        assert sync.level_a == "paragraph"
        assert sync.level_b == "sentence"


# ---- create_synchronicity_note ----


@pytest.fixture()
def sample_sync_pair() -> tuple[Synchronicity, dict[str, Fragment]]:
    """Return a sample synchronicity with its source fragments."""
    fragments = {
        "frag-a": _make_fragment(
            frag_id="frag-a",
            title="the river remembers every stone",
            platform=SourcePlatform.DISCORD,
            created=datetime(2025, 1, 5, 10, 0, 0),
        ),
        "frag-b": _make_fragment(
            frag_id="frag-b",
            title="the river remembers every stone",
            platform=SourcePlatform.JOURNAL,
            created=datetime(2025, 4, 20, 10, 0, 0),
        ),
    }
    sync = Synchronicity(
        id="sync-testnote",
        fragment_a_id="frag-a",
        fragment_b_id="frag-b",
        similarity=0.95,
        time_gap_days=105,
        source_a=SourcePlatform.DISCORD,
        source_b=SourcePlatform.JOURNAL,
    )
    return sync, fragments


class TestUnrecognisedResonanceShape:
    """Tests for the unknown-shape fall-through in ``_normalise_resonance``."""

    def test_alien_shape_is_skipped_with_warning(
        self,
        detector: SynchronicityDetector,
        cross_source_pair: dict[str, Fragment],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A non-Resonance, non-3-tuple input is logged and dropped silently."""
        # Exercise the runtime fall-through that the static signature
        # rules out: a deliberate cast to the declared input type lets
        # the runtime branch run without a bypass directive.
        alien_items: list[object] = [
            (42,),
            "not a resonance",
            ("a", "b", 0.9, "extra"),
        ]
        alien = cast("list[tuple[str, str, float]]", alien_items)
        with caplog.at_level("WARNING", logger="creek.generate.synchronicity"):
            result = detector.detect_synchronicities(alien, cross_source_pair)
        assert not result
        assert any(
            "Unrecognised resonance shape" in record.message
            for record in caplog.records
        )


class TestCreateSynchronicityNote:
    """Tests for SynchronicityDetector.create_synchronicity_note."""

    def test_writes_file(
        self,
        detector: SynchronicityDetector,
        vault_path: Path,
        sample_sync_pair: tuple[Synchronicity, dict[str, Fragment]],
    ) -> None:
        """Note should be written to disk."""
        sync, fragments = sample_sync_pair
        note_path = detector.create_synchronicity_note(sync, fragments, vault_path)
        assert note_path.exists()

    def test_writes_to_liminal_synchronicities(
        self,
        detector: SynchronicityDetector,
        vault_path: Path,
        sample_sync_pair: tuple[Synchronicity, dict[str, Fragment]],
    ) -> None:
        """Note lives under ``10-Liminal/Synchronicities/``."""
        sync, fragments = sample_sync_pair
        note_path = detector.create_synchronicity_note(sync, fragments, vault_path)
        rel = note_path.relative_to(vault_path)
        assert rel.parts[0] == "10-Liminal"
        assert rel.parts[1] == "Synchronicities"

    def test_creates_target_dir_if_missing(
        self,
        detector: SynchronicityDetector,
        tmp_path: Path,
        sample_sync_pair: tuple[Synchronicity, dict[str, Fragment]],
    ) -> None:
        """Target directory is created when it does not exist."""
        sync, fragments = sample_sync_pair
        note_path = detector.create_synchronicity_note(sync, fragments, tmp_path)
        assert note_path.exists()

    def test_frontmatter_has_type(
        self,
        detector: SynchronicityDetector,
        vault_path: Path,
        sample_sync_pair: tuple[Synchronicity, dict[str, Fragment]],
    ) -> None:
        """Frontmatter contains ``type: synchronicity``."""
        sync, fragments = sample_sync_pair
        note_path = detector.create_synchronicity_note(sync, fragments, vault_path)
        post = frontmatter.load(str(note_path))
        assert post["type"] == "synchronicity"

    def test_frontmatter_has_fragment_links(
        self,
        detector: SynchronicityDetector,
        vault_path: Path,
        sample_sync_pair: tuple[Synchronicity, dict[str, Fragment]],
    ) -> None:
        """Frontmatter lists both fragment IDs as links."""
        sync, fragments = sample_sync_pair
        note_path = detector.create_synchronicity_note(sync, fragments, vault_path)
        post = frontmatter.load(str(note_path))
        links = post["fragments"]
        assert isinstance(links, list)
        assert any("frag-a" in link for link in links)
        assert any("frag-b" in link for link in links)

    def test_frontmatter_has_similarity(
        self,
        detector: SynchronicityDetector,
        vault_path: Path,
        sample_sync_pair: tuple[Synchronicity, dict[str, Fragment]],
    ) -> None:
        """Frontmatter includes the similarity score."""
        sync, fragments = sample_sync_pair
        note_path = detector.create_synchronicity_note(sync, fragments, vault_path)
        post = frontmatter.load(str(note_path))
        assert post["similarity"] == pytest.approx(0.95)

    def test_frontmatter_has_time_gap_days(
        self,
        detector: SynchronicityDetector,
        vault_path: Path,
        sample_sync_pair: tuple[Synchronicity, dict[str, Fragment]],
    ) -> None:
        """Frontmatter records the time gap in days."""
        sync, fragments = sample_sync_pair
        note_path = detector.create_synchronicity_note(sync, fragments, vault_path)
        post = frontmatter.load(str(note_path))
        assert post["time_gap_days"] == 105

    def test_frontmatter_has_sources(
        self,
        detector: SynchronicityDetector,
        vault_path: Path,
        sample_sync_pair: tuple[Synchronicity, dict[str, Fragment]],
    ) -> None:
        """Frontmatter enumerates both source platforms."""
        sync, fragments = sample_sync_pair
        note_path = detector.create_synchronicity_note(sync, fragments, vault_path)
        post = frontmatter.load(str(note_path))
        sources = post["sources"]
        assert isinstance(sources, list)
        assert "discord" in sources
        assert "journal" in sources

    def test_body_contains_excerpts(
        self,
        detector: SynchronicityDetector,
        vault_path: Path,
        sample_sync_pair: tuple[Synchronicity, dict[str, Fragment]],
    ) -> None:
        """Body contains an excerpt from each fragment."""
        sync, fragments = sample_sync_pair
        note_path = detector.create_synchronicity_note(sync, fragments, vault_path)
        post = frontmatter.load(str(note_path))
        body = post.content
        assert "the river remembers every stone" in body

    def test_body_contains_similarity_score(
        self,
        detector: SynchronicityDetector,
        vault_path: Path,
        sample_sync_pair: tuple[Synchronicity, dict[str, Fragment]],
    ) -> None:
        """Body displays the numeric similarity score."""
        sync, fragments = sample_sync_pair
        note_path = detector.create_synchronicity_note(sync, fragments, vault_path)
        post = frontmatter.load(str(note_path))
        assert "0.95" in post.content

    def test_body_contains_time_gap(
        self,
        detector: SynchronicityDetector,
        vault_path: Path,
        sample_sync_pair: tuple[Synchronicity, dict[str, Fragment]],
    ) -> None:
        """Body mentions the time gap in days."""
        sync, fragments = sample_sync_pair
        note_path = detector.create_synchronicity_note(sync, fragments, vault_path)
        post = frontmatter.load(str(note_path))
        assert "105" in post.content

    def test_body_contains_reflection_prompt(
        self,
        detector: SynchronicityDetector,
        vault_path: Path,
        sample_sync_pair: tuple[Synchronicity, dict[str, Fragment]],
    ) -> None:
        """Body includes the reflection prompt from the ontology spec."""
        sync, fragments = sample_sync_pair
        note_path = detector.create_synchronicity_note(sync, fragments, vault_path)
        post = frontmatter.load(str(note_path))
        assert "pattern is trying to surface" in post.content

    def test_body_tags_with_synchronicity(
        self,
        detector: SynchronicityDetector,
        vault_path: Path,
        sample_sync_pair: tuple[Synchronicity, dict[str, Fragment]],
    ) -> None:
        """The literal ``#synchronicity`` tag appears in the note body."""
        sync, fragments = sample_sync_pair
        note_path = detector.create_synchronicity_note(sync, fragments, vault_path)
        post = frontmatter.load(str(note_path))
        assert "#synchronicity" in post.content

    def test_filename_uses_sync_id(
        self,
        detector: SynchronicityDetector,
        vault_path: Path,
        sample_sync_pair: tuple[Synchronicity, dict[str, Fragment]],
    ) -> None:
        """Filename embeds the synchronicity ID for uniqueness."""
        sync, fragments = sample_sync_pair
        note_path = detector.create_synchronicity_note(sync, fragments, vault_path)
        assert sync.id in note_path.name
        assert note_path.suffix == ".md"

    def test_missing_fragment_raises(
        self,
        detector: SynchronicityDetector,
        vault_path: Path,
    ) -> None:
        """Creating a note requires both referenced fragments to be present."""
        sync = Synchronicity(
            fragment_a_id="frag-a",
            fragment_b_id="frag-b",
            similarity=0.95,
            time_gap_days=100,
            source_a=SourcePlatform.DISCORD,
            source_b=SourcePlatform.JOURNAL,
        )
        with pytest.raises(KeyError):
            detector.create_synchronicity_note(sync, {}, vault_path)
