"""Tests for creek.generate.synchronicity — synchronicity detection.

Tests cover the SynchronicityDetector class and its two public methods:
detect_synchronicities (filtering resonances against synchronicity criteria)
and create_synchronicity_note (writing reflection notes to
``10-Liminal/Synchronicities/``).
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

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
    """Construct a test Fragment with the given identifying fields."""
    return Fragment(
        id=frag_id,
        title=title,
        source=FragmentSource(platform=platform),
        created=created,
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


# ---- FEAT-024 cross-level ranking ----


def _level_fragment(
    *,
    frag_id: str,
    title: str,
    platform: SourcePlatform,
    created: datetime,
    level: str,
) -> Fragment:
    """Construct a test Fragment with an explicit structural level."""
    return Fragment(
        id=frag_id,
        title=title,
        source=FragmentSource(platform=platform),
        created=created,
        level=level,  # type: ignore[arg-type]
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
