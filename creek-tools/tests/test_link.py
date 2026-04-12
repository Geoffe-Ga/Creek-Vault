"""Tests for creek.link module — linking pipeline and component linkers.

Tests cover EmbeddingLinker, TemporalLinker (with TemporalLink scoring),
ThreadDetector, EddyDetector, LinkingResult, and LinkingPipeline orchestration.
"""

import logging
from datetime import datetime, timedelta
from pathlib import Path

from creek.config import EmbeddingsConfig, LinkingConfig
from creek.link import (
    EddyDetector,
    EmbeddingLinker,
    LinkingPipeline,
    LinkingResult,
    TemporalLink,
    TemporalLinker,
    ThreadDetector,
)
from creek.link.eddies import EddyDetector as EddyDetectorDirect
from creek.link.embeddings import EmbeddingLinker as EmbeddingLinkerDirect
from creek.link.linker import LinkingPipeline as LinkingPipelineDirect
from creek.link.linker import LinkingResult as LinkingResultDirect
from creek.link.temporal import TemporalLink as TemporalLinkDirect
from creek.link.temporal import TemporalLinker as TemporalLinkerDirect
from creek.link.threads import ThreadDetector as ThreadDetectorDirect
from creek.models import (
    Fragment,
    FragmentSource,
    Frequency,
    FrequencyClassification,
    Mode,
    Phase,
    SourcePlatform,
    Thread,
    ThreadStatus,
    WavelengthClassification,
)


def _make_fragment(
    title: str = "Test Fragment",
    platform: SourcePlatform = SourcePlatform.CLAUDE,
    created: datetime | None = None,
    primary_freq: Frequency = Frequency.UNCLASSIFIED,
    secondary_freqs: list[Frequency] | None = None,
    phase: Phase = Phase.UNCLASSIFIED,
    mode: Mode = Mode.UNCLASSIFIED,
    emotional_texture: list[str] | None = None,
) -> Fragment:
    """Create a Fragment for testing with configurable classification fields."""
    return Fragment(
        title=title,
        source=FragmentSource(platform=platform),
        created=created or datetime.now(),
        frequency=FrequencyClassification(
            primary=primary_freq,
            secondary=secondary_freqs or [],
        ),
        wavelength=WavelengthClassification(phase=phase, mode=mode),
        emotional_texture=emotional_texture or [],
    )


# ---- Package __init__ re-exports ----


class TestPackageExports:
    """Tests that creek.link.__init__ re-exports all public classes."""

    def test_embedding_linker_reexported(self) -> None:
        """EmbeddingLinker should be importable from creek.link."""
        assert EmbeddingLinker is EmbeddingLinkerDirect

    def test_temporal_link_reexported(self) -> None:
        """TemporalLink should be importable from creek.link."""
        assert TemporalLink is TemporalLinkDirect

    def test_temporal_linker_reexported(self) -> None:
        """TemporalLinker should be importable from creek.link."""
        assert TemporalLinker is TemporalLinkerDirect

    def test_thread_detector_reexported(self) -> None:
        """ThreadDetector should be importable from creek.link."""
        assert ThreadDetector is ThreadDetectorDirect

    def test_eddy_detector_reexported(self) -> None:
        """EddyDetector should be importable from creek.link."""
        assert EddyDetector is EddyDetectorDirect

    def test_linking_result_reexported(self) -> None:
        """LinkingResult should be importable from creek.link."""
        assert LinkingResult is LinkingResultDirect

    def test_linking_pipeline_reexported(self) -> None:
        """LinkingPipeline should be importable from creek.link."""
        assert LinkingPipeline is LinkingPipelineDirect


# ---- EmbeddingLinker Tests ----


class TestEmbeddingLinker:
    """Tests for the EmbeddingLinker class."""

    def test_init_stores_config(self) -> None:
        """EmbeddingLinker should store the provided EmbeddingsConfig."""
        config = EmbeddingsConfig(model="test-model", similarity_threshold=0.8)
        linker = EmbeddingLinker(config=config)
        assert linker.config is config

    def test_generate_embeddings_returns_dict(self) -> None:
        """generate_embeddings should return a dict keyed by fragment IDs."""
        config = EmbeddingsConfig()
        linker = EmbeddingLinker(config=config)
        fragments = [_make_fragment("A"), _make_fragment("B")]
        result = linker.generate_embeddings(fragments)
        assert isinstance(result, dict)
        assert len(result) == 2

    def test_generate_embeddings_empty_input(self) -> None:
        """generate_embeddings with empty list should return empty dict."""
        config = EmbeddingsConfig()
        linker = EmbeddingLinker(config=config)
        result = linker.generate_embeddings([])
        assert result == {}

    def test_find_resonances_below_threshold_returns_empty(self) -> None:
        """find_resonances should return empty for dissimilar vectors."""
        config = EmbeddingsConfig(similarity_threshold=0.99)
        linker = EmbeddingLinker(config=config)
        embeddings: dict[str, list[float]] = {
            "frag-1": [1.0, 0.0, 0.0],
            "frag-2": [0.0, 1.0, 0.0],
        }
        result = linker.find_resonances(embeddings)
        assert result == []
        assert isinstance(result, list)

    def test_find_resonances_empty_input(self) -> None:
        """find_resonances with empty dict should return empty list."""
        config = EmbeddingsConfig()
        linker = EmbeddingLinker(config=config)
        result = linker.find_resonances({})
        assert result == []

    def test_generate_embeddings_logs_message(self, caplog) -> None:
        """generate_embeddings should log an info message."""
        config = EmbeddingsConfig()
        linker = EmbeddingLinker(config=config)
        fragments = [_make_fragment("A")]
        with caplog.at_level(logging.INFO, logger="creek.link.embeddings"):
            linker.generate_embeddings(fragments)
        assert any("embedding" in r.message.lower() for r in caplog.records)

    def test_find_resonances_logs_message(self, caplog) -> None:
        """find_resonances should log an info message."""
        config = EmbeddingsConfig()
        linker = EmbeddingLinker(config=config)
        with caplog.at_level(logging.INFO, logger="creek.link.embeddings"):
            linker.find_resonances({"frag-1": [0.1], "frag-2": [0.2]})
        assert any("resonance" in r.message.lower() for r in caplog.records)


# ---- TemporalLink Model Tests ----


class TestTemporalLink:
    """Tests for the TemporalLink Pydantic model."""

    def test_creation_with_all_fields(self) -> None:
        """TemporalLink should accept all required fields."""
        link = TemporalLink(
            fragment_a_id="frag-aaaaaaaa",
            fragment_b_id="frag-bbbbbbbb",
            time_delta_hours=24.0,
            overlap_score=0.6,
            shared_dimensions=["primary_frequency", "wavelength_phase"],
        )
        assert link.fragment_a_id == "frag-aaaaaaaa"
        assert link.fragment_b_id == "frag-bbbbbbbb"
        assert link.time_delta_hours == 24.0
        assert link.overlap_score == 0.6
        assert link.shared_dimensions == ["primary_frequency", "wavelength_phase"]

    def test_model_dump(self) -> None:
        """TemporalLink model_dump should produce a serializable dict."""
        link = TemporalLink(
            fragment_a_id="frag-a",
            fragment_b_id="frag-b",
            time_delta_hours=12.5,
            overlap_score=0.3,
            shared_dimensions=["mode"],
        )
        dump = link.model_dump()
        assert dump["fragment_a_id"] == "frag-a"
        assert dump["overlap_score"] == 0.3
        assert dump["shared_dimensions"] == ["mode"]

    def test_empty_shared_dimensions(self) -> None:
        """TemporalLink should allow empty shared_dimensions list."""
        link = TemporalLink(
            fragment_a_id="frag-a",
            fragment_b_id="frag-b",
            time_delta_hours=1.0,
            overlap_score=0.2,
            shared_dimensions=[],
        )
        assert link.shared_dimensions == []


# ---- TemporalLinker Tests ----


class TestTemporalLinker:
    """Tests for the TemporalLinker temporal proximity linking."""

    def test_empty_input_returns_empty_list(self) -> None:
        """find_temporal_links with empty list should return empty list."""
        linker = TemporalLinker()
        result = linker.find_temporal_links([], window_hours=168)
        assert result == []

    def test_single_fragment_returns_empty_list(self) -> None:
        """A single fragment cannot form a pair."""
        linker = TemporalLinker()
        fragments = [_make_fragment("A")]
        result = linker.find_temporal_links(fragments, window_hours=168)
        assert result == []

    def test_same_source_no_links(self) -> None:
        """Fragments from the same source should not be linked."""
        now = datetime.now()
        linker = TemporalLinker()
        fragments = [
            _make_fragment("A", platform=SourcePlatform.CLAUDE, created=now),
            _make_fragment("B", platform=SourcePlatform.CLAUDE, created=now),
        ]
        result = linker.find_temporal_links(fragments, window_hours=168)
        assert result == []

    def test_cross_source_within_window_produces_link(self) -> None:
        """Fragments from different sources within the window should link."""
        now = datetime.now()
        linker = TemporalLinker()
        fragments = [
            _make_fragment(
                "A",
                platform=SourcePlatform.CLAUDE,
                created=now,
                primary_freq=Frequency.F1,
                phase=Phase.RISING,
            ),
            _make_fragment(
                "B",
                platform=SourcePlatform.DISCORD,
                created=now + timedelta(hours=12),
                primary_freq=Frequency.F1,
                phase=Phase.RISING,
            ),
        ]
        result = linker.find_temporal_links(fragments, window_hours=168)
        assert len(result) == 1
        assert isinstance(result[0], TemporalLink)

    def test_outside_window_no_link(self) -> None:
        """Fragments outside the time window should not link."""
        now = datetime.now()
        linker = TemporalLinker()
        fragments = [
            _make_fragment(
                "A",
                platform=SourcePlatform.CLAUDE,
                created=now,
                primary_freq=Frequency.F1,
            ),
            _make_fragment(
                "B",
                platform=SourcePlatform.DISCORD,
                created=now + timedelta(hours=200),
                primary_freq=Frequency.F1,
            ),
        ]
        result = linker.find_temporal_links(fragments, window_hours=168)
        assert result == []

    def test_custom_window_hours(self) -> None:
        """Custom window_hours parameter should be respected."""
        now = datetime.now()
        linker = TemporalLinker()
        fragments = [
            _make_fragment(
                "A",
                platform=SourcePlatform.CLAUDE,
                created=now,
                primary_freq=Frequency.F3,
            ),
            _make_fragment(
                "B",
                platform=SourcePlatform.DISCORD,
                created=now + timedelta(hours=30),
                primary_freq=Frequency.F3,
            ),
        ]
        # 24-hour window — fragments are 30 hours apart
        result = linker.find_temporal_links(fragments, window_hours=24)
        assert result == []

        # 48-hour window — fragments are 30 hours apart
        result = linker.find_temporal_links(fragments, window_hours=48)
        assert len(result) == 1

    def test_time_delta_hours_is_correct(self) -> None:
        """TemporalLink time_delta_hours should reflect actual gap."""
        now = datetime.now()
        linker = TemporalLinker()
        fragments = [
            _make_fragment(
                "A",
                platform=SourcePlatform.CLAUDE,
                created=now,
                primary_freq=Frequency.F1,
                phase=Phase.RISING,
            ),
            _make_fragment(
                "B",
                platform=SourcePlatform.DISCORD,
                created=now + timedelta(hours=36),
                primary_freq=Frequency.F1,
                phase=Phase.RISING,
            ),
        ]
        result = linker.find_temporal_links(fragments, window_hours=168)
        assert len(result) == 1
        assert result[0].time_delta_hours == 36.0

    def test_score_same_primary_frequency(self) -> None:
        """Same primary frequency should add +0.3 to overlap_score."""
        now = datetime.now()
        linker = TemporalLinker()
        fragments = [
            _make_fragment(
                "A",
                platform=SourcePlatform.CLAUDE,
                created=now,
                primary_freq=Frequency.F5,
            ),
            _make_fragment(
                "B",
                platform=SourcePlatform.DISCORD,
                created=now,
                primary_freq=Frequency.F5,
            ),
        ]
        result = linker.find_temporal_links(fragments, window_hours=168)
        assert len(result) == 1
        # +0.3 (same primary freq) + 0.2 (different source) = 0.5
        assert result[0].overlap_score == 0.5
        assert "primary_frequency" in result[0].shared_dimensions

    def test_score_shared_secondary_frequency(self) -> None:
        """Shared secondary frequencies should add +0.1 each."""
        now = datetime.now()
        linker = TemporalLinker()
        fragments = [
            _make_fragment(
                "A",
                platform=SourcePlatform.CLAUDE,
                created=now,
                secondary_freqs=[Frequency.F2, Frequency.F3],
            ),
            _make_fragment(
                "B",
                platform=SourcePlatform.DISCORD,
                created=now,
                secondary_freqs=[Frequency.F2, Frequency.F4],
            ),
        ]
        result = linker.find_temporal_links(fragments, window_hours=168)
        assert len(result) == 1
        # +0.1 (one shared secondary F2) + 0.2 (different source) = 0.3
        assert result[0].overlap_score == 0.3
        assert "secondary_frequency" in result[0].shared_dimensions

    def test_score_same_wavelength_phase(self) -> None:
        """Same wavelength phase should add +0.2 to overlap_score."""
        now = datetime.now()
        linker = TemporalLinker()
        fragments = [
            _make_fragment(
                "A",
                platform=SourcePlatform.CLAUDE,
                created=now,
                phase=Phase.PEAKING,
            ),
            _make_fragment(
                "B",
                platform=SourcePlatform.DISCORD,
                created=now,
                phase=Phase.PEAKING,
            ),
        ]
        result = linker.find_temporal_links(fragments, window_hours=168)
        assert len(result) == 1
        # +0.2 (same phase) + 0.2 (different source) = 0.4
        assert result[0].overlap_score == 0.4
        assert "wavelength_phase" in result[0].shared_dimensions

    def test_score_same_mode(self) -> None:
        """Same mode should add +0.1 to overlap_score."""
        now = datetime.now()
        linker = TemporalLinker()
        fragments = [
            _make_fragment(
                "A",
                platform=SourcePlatform.CLAUDE,
                created=now,
                mode=Mode.EXPRESS,
            ),
            _make_fragment(
                "B",
                platform=SourcePlatform.DISCORD,
                created=now,
                mode=Mode.EXPRESS,
            ),
        ]
        result = linker.find_temporal_links(fragments, window_hours=168)
        assert len(result) == 1
        # +0.1 (same mode) + 0.2 (different source) = 0.3
        assert result[0].overlap_score == 0.3
        assert "mode" in result[0].shared_dimensions

    def test_score_shared_emotional_texture(self) -> None:
        """Shared emotional_texture tags should add +0.1 each."""
        now = datetime.now()
        linker = TemporalLinker()
        fragments = [
            _make_fragment(
                "A",
                platform=SourcePlatform.CLAUDE,
                created=now,
                emotional_texture=["joy", "curiosity", "awe"],
            ),
            _make_fragment(
                "B",
                platform=SourcePlatform.DISCORD,
                created=now,
                emotional_texture=["joy", "curiosity", "grief"],
            ),
        ]
        result = linker.find_temporal_links(fragments, window_hours=168)
        assert len(result) == 1
        # +0.2 (2 shared textures) + 0.2 (different source) = 0.4
        assert result[0].overlap_score == 0.4
        assert "emotional_texture" in result[0].shared_dimensions

    def test_score_different_source_bonus(self) -> None:
        """Different source platforms should add +0.2 to overlap_score."""
        now = datetime.now()
        linker = TemporalLinker()
        # Two fragments with no shared dimensions except cross-source
        fragments = [
            _make_fragment("A", platform=SourcePlatform.CLAUDE, created=now),
            _make_fragment("B", platform=SourcePlatform.DISCORD, created=now),
        ]
        result = linker.find_temporal_links(fragments, window_hours=168)
        # Only +0.2 for different source — below default threshold
        assert result == []

    def test_combined_scoring(self) -> None:
        """All scoring dimensions should combine correctly."""
        now = datetime.now()
        linker = TemporalLinker()
        fragments = [
            _make_fragment(
                "A",
                platform=SourcePlatform.CLAUDE,
                created=now,
                primary_freq=Frequency.F1,
                secondary_freqs=[Frequency.F2],
                phase=Phase.RISING,
                mode=Mode.INHABIT,
                emotional_texture=["joy"],
            ),
            _make_fragment(
                "B",
                platform=SourcePlatform.DISCORD,
                created=now + timedelta(hours=1),
                primary_freq=Frequency.F1,
                secondary_freqs=[Frequency.F2, Frequency.F3],
                phase=Phase.RISING,
                mode=Mode.INHABIT,
                emotional_texture=["joy", "wonder"],
            ),
        ]
        result = linker.find_temporal_links(fragments, window_hours=168)
        assert len(result) == 1
        link = result[0]
        # +0.3 (primary freq) + 0.1 (1 shared secondary F2)
        # +0.2 (same phase) + 0.1 (same mode)
        # +0.1 (1 shared emotional texture "joy")
        # +0.2 (different source)
        expected_score = 0.3 + 0.1 + 0.2 + 0.1 + 0.1 + 0.2
        assert abs(link.overlap_score - expected_score) < 1e-9

    def test_threshold_filtering(self) -> None:
        """Links below the combined threshold should be excluded."""
        now = datetime.now()
        linker = TemporalLinker(min_score=0.5)
        # Only different source (+0.2) — below 0.5 threshold
        fragments = [
            _make_fragment("A", platform=SourcePlatform.CLAUDE, created=now),
            _make_fragment("B", platform=SourcePlatform.DISCORD, created=now),
        ]
        result = linker.find_temporal_links(fragments, window_hours=168)
        assert result == []

    def test_threshold_includes_at_boundary(self) -> None:
        """Links at exactly the threshold should be included."""
        now = datetime.now()
        linker = TemporalLinker(min_score=0.5)
        fragments = [
            _make_fragment(
                "A",
                platform=SourcePlatform.CLAUDE,
                created=now,
                primary_freq=Frequency.F1,
            ),
            _make_fragment(
                "B",
                platform=SourcePlatform.DISCORD,
                created=now,
                primary_freq=Frequency.F1,
            ),
        ]
        result = linker.find_temporal_links(fragments, window_hours=168)
        # +0.3 (primary) + 0.2 (cross-source) = 0.5 — exactly at threshold
        assert len(result) == 1

    def test_multiple_pairs(self) -> None:
        """Multiple qualifying pairs within window should all be returned."""
        now = datetime.now()
        linker = TemporalLinker(min_score=0.3)
        fragments = [
            _make_fragment(
                "A",
                platform=SourcePlatform.CLAUDE,
                created=now,
                primary_freq=Frequency.F1,
            ),
            _make_fragment(
                "B",
                platform=SourcePlatform.DISCORD,
                created=now + timedelta(hours=1),
                primary_freq=Frequency.F1,
            ),
            _make_fragment(
                "C",
                platform=SourcePlatform.JOURNAL,
                created=now + timedelta(hours=2),
                primary_freq=Frequency.F1,
            ),
        ]
        result = linker.find_temporal_links(fragments, window_hours=168)
        # A-B, A-C, B-C all cross-source with same freq
        assert len(result) == 3

    def test_unclassified_dimensions_do_not_score(self) -> None:
        """UNCLASSIFIED values should not contribute to scoring."""
        now = datetime.now()
        linker = TemporalLinker()
        fragments = [
            _make_fragment(
                "A",
                platform=SourcePlatform.CLAUDE,
                created=now,
                primary_freq=Frequency.UNCLASSIFIED,
                phase=Phase.UNCLASSIFIED,
                mode=Mode.UNCLASSIFIED,
            ),
            _make_fragment(
                "B",
                platform=SourcePlatform.DISCORD,
                created=now,
                primary_freq=Frequency.UNCLASSIFIED,
                phase=Phase.UNCLASSIFIED,
                mode=Mode.UNCLASSIFIED,
            ),
        ]
        result = linker.find_temporal_links(fragments, window_hours=168)
        # Only +0.2 for cross-source, below default threshold
        assert result == []

    def test_returns_list_of_temporal_links(self) -> None:
        """Return type should be list[TemporalLink]."""
        now = datetime.now()
        linker = TemporalLinker(min_score=0.0)
        fragments = [
            _make_fragment("A", platform=SourcePlatform.CLAUDE, created=now),
            _make_fragment("B", platform=SourcePlatform.DISCORD, created=now),
        ]
        result = linker.find_temporal_links(fragments, window_hours=168)
        assert len(result) == 1
        assert isinstance(result[0], TemporalLink)

    def test_fragment_ids_in_link(self) -> None:
        """TemporalLink should contain the correct fragment IDs."""
        now = datetime.now()
        linker = TemporalLinker(min_score=0.0)
        frag_a = _make_fragment(
            "A",
            platform=SourcePlatform.CLAUDE,
            created=now,
        )
        frag_b = _make_fragment(
            "B",
            platform=SourcePlatform.DISCORD,
            created=now,
        )
        result = linker.find_temporal_links([frag_a, frag_b], window_hours=168)
        assert len(result) == 1
        ids = {result[0].fragment_a_id, result[0].fragment_b_id}
        assert ids == {frag_a.id, frag_b.id}

    def test_logs_info_message(self, caplog) -> None:
        """find_temporal_links should log an info message."""
        linker = TemporalLinker()
        fragments = [_make_fragment("A")]
        with caplog.at_level(logging.INFO, logger="creek.link.temporal"):
            linker.find_temporal_links(fragments, window_hours=168)
        assert any("temporal" in r.message.lower() for r in caplog.records)


# ---- ThreadDetector Tests ----


class TestThreadDetector:
    """Tests for the ThreadDetector class (sliding window + topic consistency)."""

    def test_detect_threads_below_min_fragments_returns_empty_list(self) -> None:
        """Two fragments with default ``min_fragments=3`` yield no threads."""
        detector = ThreadDetector()
        fragments = [_make_fragment("A"), _make_fragment("B")]
        result = detector.detect_threads(fragments)
        assert result == []
        assert isinstance(result, list)

    def test_detect_threads_empty_input(self) -> None:
        """detect_threads with empty list should return empty list."""
        detector = ThreadDetector()
        result = detector.detect_threads([])
        assert result == []

    def test_detect_threads_logs_message(self, caplog) -> None:
        """detect_threads should log an info message."""
        detector = ThreadDetector()
        fragments = [_make_fragment("A")]
        with caplog.at_level(logging.INFO, logger="creek.link.threads"):
            detector.detect_threads(fragments)
        assert any("thread" in r.message.lower() for r in caplog.records)

    def test_detect_threads_simple_cluster(self) -> None:
        """Three frequency-aligned fragments in window form one thread."""
        now = datetime(2026, 4, 1, 12, 0, 0)
        detector = ThreadDetector(now=now)
        fragments = [
            _make_fragment(
                "Reading Practice",
                created=now - timedelta(days=10),
                primary_freq=Frequency.F4,
            ),
            _make_fragment(
                "Reading Habits",
                created=now - timedelta(days=5),
                primary_freq=Frequency.F4,
            ),
            _make_fragment(
                "Reading Notes",
                created=now - timedelta(days=1),
                primary_freq=Frequency.F4,
            ),
        ]
        result = detector.detect_threads(fragments)
        assert len(result) == 1
        thread = result[0]
        assert thread.fragment_count == 3
        assert thread.first_seen == (now - timedelta(days=10)).date()
        assert thread.last_seen == (now - timedelta(days=1)).date()

    def test_detect_threads_respects_min_fragments_argument(self) -> None:
        """min_fragments=2 admits clusters with two members."""
        now = datetime(2026, 4, 1)
        detector = ThreadDetector(now=now)
        fragments = [
            _make_fragment(
                "Hiking Logs",
                created=now - timedelta(days=5),
                primary_freq=Frequency.F2,
            ),
            _make_fragment(
                "Hiking Trips",
                created=now - timedelta(days=2),
                primary_freq=Frequency.F2,
            ),
        ]
        result = detector.detect_threads(fragments, min_fragments=2)
        assert len(result) == 1
        assert result[0].fragment_count == 2

    def test_detect_threads_excludes_unconnected_fragments(self) -> None:
        """Fragments with non-overlapping frequencies stay separate."""
        now = datetime(2026, 4, 1)
        detector = ThreadDetector(now=now)
        fragments = [
            _make_fragment(
                "Topic A One",
                created=now - timedelta(days=10),
                primary_freq=Frequency.F1,
            ),
            _make_fragment(
                "Topic A Two",
                created=now - timedelta(days=5),
                primary_freq=Frequency.F1,
            ),
            _make_fragment(
                "Topic A Three",
                created=now - timedelta(days=1),
                primary_freq=Frequency.F1,
            ),
            _make_fragment(
                "Different",
                created=now - timedelta(days=2),
                primary_freq=Frequency.F9,
            ),
        ]
        result = detector.detect_threads(fragments)
        assert len(result) == 1
        assert result[0].fragment_count == 3

    def test_detect_threads_secondary_frequency_overlap_links(self) -> None:
        """Fragments with overlapping secondary frequencies connect."""
        now = datetime(2026, 4, 1)
        detector = ThreadDetector(now=now)
        fragments = [
            _make_fragment(
                "Alpha",
                created=now - timedelta(days=10),
                primary_freq=Frequency.F1,
                secondary_freqs=[Frequency.F5],
            ),
            _make_fragment(
                "Beta",
                created=now - timedelta(days=5),
                primary_freq=Frequency.F2,
                secondary_freqs=[Frequency.F5],
            ),
            _make_fragment(
                "Gamma",
                created=now - timedelta(days=1),
                primary_freq=Frequency.F3,
                secondary_freqs=[Frequency.F5],
            ),
        ]
        result = detector.detect_threads(fragments)
        assert len(result) == 1

    def test_detect_threads_outside_window_does_not_link(self) -> None:
        """Fragments separated by more than window_days are not linked directly."""
        now = datetime(2026, 4, 1)
        detector = ThreadDetector(now=now, window_days=10)
        fragments = [
            _make_fragment(
                "A",
                created=now - timedelta(days=100),
                primary_freq=Frequency.F1,
            ),
            _make_fragment(
                "B",
                created=now - timedelta(days=80),
                primary_freq=Frequency.F1,
            ),
            _make_fragment(
                "C",
                created=now - timedelta(days=60),
                primary_freq=Frequency.F1,
            ),
        ]
        result = detector.detect_threads(fragments)
        assert result == []

    def test_detect_threads_chains_via_union_find_across_windows(self) -> None:
        """Overlapping windows transitively merge clusters via union-find."""
        now = datetime(2026, 4, 1)
        detector = ThreadDetector(now=now, window_days=20)
        # Fragments at days -40, -25, -10 — A↔B (15d apart) and B↔C (15d
        # apart) both within the 20-day window, but A↔C (30d) would not
        # connect on its own. Union-find must chain them via B.
        fragments = [
            _make_fragment(
                "Anchor One",
                created=now - timedelta(days=40),
                primary_freq=Frequency.F1,
            ),
            _make_fragment(
                "Anchor Two",
                created=now - timedelta(days=25),
                primary_freq=Frequency.F1,
            ),
            _make_fragment(
                "Anchor Three",
                created=now - timedelta(days=10),
                primary_freq=Frequency.F1,
            ),
        ]
        result = detector.detect_threads(fragments)
        assert len(result) == 1
        assert result[0].fragment_count == 3

    def test_detect_threads_status_active(self) -> None:
        """Last seen within ACTIVE_DAYS yields ACTIVE status."""
        now = datetime(2026, 4, 1)
        detector = ThreadDetector(now=now)
        fragments = [
            _make_fragment(
                f"Recent {i}",
                created=now - timedelta(days=10 - i),
                primary_freq=Frequency.F1,
            )
            for i in range(3)
        ]
        result = detector.detect_threads(fragments)
        assert len(result) == 1
        assert result[0].status == ThreadStatus.ACTIVE.value

    def test_detect_threads_status_dormant(self) -> None:
        """Last seen between 30 and 180 days yields DORMANT status."""
        now = datetime(2026, 4, 1)
        detector = ThreadDetector(now=now, window_days=60)
        fragments = [
            _make_fragment(
                f"Older {i}",
                created=now - timedelta(days=120 - i * 10),
                primary_freq=Frequency.F1,
            )
            for i in range(3)
        ]
        result = detector.detect_threads(fragments)
        assert len(result) == 1
        assert result[0].status == ThreadStatus.DORMANT.value

    def test_detect_threads_status_resolved(self) -> None:
        """Last seen more than 180 days ago yields RESOLVED status."""
        now = datetime(2026, 4, 1)
        detector = ThreadDetector(now=now, window_days=120)
        fragments = [
            _make_fragment(
                f"Ancient {i}",
                created=now - timedelta(days=400 - i * 30),
                primary_freq=Frequency.F1,
            )
            for i in range(3)
        ]
        result = detector.detect_threads(fragments)
        assert len(result) == 1
        assert result[0].status == ThreadStatus.RESOLVED.value

    def test_detect_threads_title_from_common_keywords(self) -> None:
        """Titles are built from the most common non-stopword keywords."""
        now = datetime(2026, 4, 1)
        detector = ThreadDetector(now=now)
        fragments = [
            _make_fragment(
                "The Garden Project Begins",
                created=now - timedelta(days=10),
                primary_freq=Frequency.F1,
            ),
            _make_fragment(
                "Garden Update Notes",
                created=now - timedelta(days=5),
                primary_freq=Frequency.F1,
            ),
            _make_fragment(
                "Garden Harvest Reflection",
                created=now - timedelta(days=1),
                primary_freq=Frequency.F1,
            ),
        ]
        result = detector.detect_threads(fragments)
        assert len(result) == 1
        assert "Garden" in result[0].title

    def test_detect_threads_title_falls_back_to_first_fragment(self) -> None:
        """No content words → fallback to truncated first-fragment title."""
        now = datetime(2026, 4, 1)
        detector = ThreadDetector(now=now)
        fragments = [
            _make_fragment(
                "X",
                created=now - timedelta(days=10),
                primary_freq=Frequency.F1,
            ),
            _make_fragment(
                "Y",
                created=now - timedelta(days=5),
                primary_freq=Frequency.F1,
            ),
            _make_fragment(
                "Z",
                created=now - timedelta(days=1),
                primary_freq=Frequency.F1,
            ),
        ]
        result = detector.detect_threads(fragments)
        assert len(result) == 1
        # First fragment by created order is "X" (oldest)
        assert result[0].title == "X"

    def test_detect_threads_frequency_affinity_picks_most_common(self) -> None:
        """Frequency affinity reflects the dominant primary frequency(s)."""
        now = datetime(2026, 4, 1)
        detector = ThreadDetector(now=now)
        fragments = [
            _make_fragment(
                "One",
                created=now - timedelta(days=10),
                primary_freq=Frequency.F2,
                secondary_freqs=[Frequency.F7],
            ),
            _make_fragment(
                "Two",
                created=now - timedelta(days=5),
                primary_freq=Frequency.F2,
                secondary_freqs=[Frequency.F7],
            ),
            _make_fragment(
                "Three",
                created=now - timedelta(days=1),
                primary_freq=Frequency.F2,
                secondary_freqs=[Frequency.F7],
            ),
        ]
        result = detector.detect_threads(fragments)
        assert len(result) == 1
        assert Frequency.F2.value in result[0].frequency_affinity

    def test_detect_threads_uses_embeddings_when_provided(self) -> None:
        """High-similarity embeddings link frequency-aligned fragments."""
        now = datetime(2026, 4, 1)
        embeddings = {
            "frag-1": [1.0, 0.0, 0.0],
            "frag-2": [1.0, 0.0, 0.0],
            "frag-3": [1.0, 0.0, 0.0],
        }
        detector = ThreadDetector(embeddings=embeddings, now=now)
        fragments = [
            Fragment(
                id="frag-1",
                title="Embeds One",
                source=FragmentSource(platform=SourcePlatform.CLAUDE),
                created=now - timedelta(days=10),
                frequency=FrequencyClassification(primary=Frequency.F1),
            ),
            Fragment(
                id="frag-2",
                title="Embeds Two",
                source=FragmentSource(platform=SourcePlatform.CLAUDE),
                created=now - timedelta(days=5),
                frequency=FrequencyClassification(primary=Frequency.F1),
            ),
            Fragment(
                id="frag-3",
                title="Embeds Three",
                source=FragmentSource(platform=SourcePlatform.CLAUDE),
                created=now - timedelta(days=1),
                frequency=FrequencyClassification(primary=Frequency.F1),
            ),
        ]
        result = detector.detect_threads(fragments)
        assert len(result) == 1

    def test_detect_threads_low_embedding_similarity_blocks_link(self) -> None:
        """Orthogonal embeddings prevent topic consistency even with shared freq."""
        now = datetime(2026, 4, 1)
        embeddings = {
            "frag-1": [1.0, 0.0, 0.0],
            "frag-2": [0.0, 1.0, 0.0],
            "frag-3": [0.0, 0.0, 1.0],
        }
        detector = ThreadDetector(embeddings=embeddings, now=now)
        fragments = [
            Fragment(
                id="frag-1",
                title="Alpha",
                source=FragmentSource(platform=SourcePlatform.CLAUDE),
                created=now - timedelta(days=10),
                frequency=FrequencyClassification(primary=Frequency.F1),
            ),
            Fragment(
                id="frag-2",
                title="Beta",
                source=FragmentSource(platform=SourcePlatform.CLAUDE),
                created=now - timedelta(days=5),
                frequency=FrequencyClassification(primary=Frequency.F1),
            ),
            Fragment(
                id="frag-3",
                title="Gamma",
                source=FragmentSource(platform=SourcePlatform.CLAUDE),
                created=now - timedelta(days=1),
                frequency=FrequencyClassification(primary=Frequency.F1),
            ),
        ]
        result = detector.detect_threads(fragments)
        assert result == []

    def test_detect_threads_unclassified_frequency_does_not_link(self) -> None:
        """UNCLASSIFIED frequencies provide no signal for clustering."""
        now = datetime(2026, 4, 1)
        detector = ThreadDetector(now=now)
        fragments = [
            _make_fragment(
                f"Unclassified {i}",
                created=now - timedelta(days=10 - i),
                primary_freq=Frequency.UNCLASSIFIED,
            )
            for i in range(3)
        ]
        result = detector.detect_threads(fragments)
        assert result == []

    def test_detect_threads_threads_sorted_by_first_seen(self) -> None:
        """Returned threads are ordered by ``first_seen`` ascending."""
        now = datetime(2026, 4, 1)
        detector = ThreadDetector(now=now, window_days=10)
        fragments = [
            # Cluster A around -100 days (frequency F1)
            _make_fragment(
                f"Old A {i}",
                created=now - timedelta(days=100 - i),
                primary_freq=Frequency.F1,
            )
            for i in range(3)
        ] + [
            # Cluster B around -10 days (frequency F2)
            _make_fragment(
                f"New B {i}",
                created=now - timedelta(days=10 - i),
                primary_freq=Frequency.F2,
            )
            for i in range(3)
        ]
        result = detector.detect_threads(fragments)
        assert len(result) == 2
        assert result[0].first_seen < result[1].first_seen

    def test_thread_members_property_exposes_membership(self) -> None:
        """The ``thread_members`` property reports detected memberships."""
        now = datetime(2026, 4, 1)
        detector = ThreadDetector(now=now)
        fragments = [
            _make_fragment(
                f"Member {i}",
                created=now - timedelta(days=10 - i),
                primary_freq=Frequency.F1,
            )
            for i in range(3)
        ]
        threads = detector.detect_threads(fragments)
        assert len(threads) == 1
        members = detector.thread_members[threads[0].id]
        assert set(members) == {f.id for f in fragments}

    def test_thread_members_resets_on_new_detection(self) -> None:
        """A fresh detect_threads run replaces any prior membership map."""
        now = datetime(2026, 4, 1)
        detector = ThreadDetector(now=now)
        first = [
            _make_fragment(
                f"First {i}",
                created=now - timedelta(days=10 - i),
                primary_freq=Frequency.F1,
            )
            for i in range(3)
        ]
        detector.detect_threads(first)
        # Second run with too few fragments should clear membership
        detector.detect_threads([_make_fragment("Solo")])
        assert detector.thread_members == {}


class TestThreadAssignment:
    """Tests for assign_fragments_to_threads."""

    def test_assignment_adds_wikilinks(self) -> None:
        """Fragments belonging to a thread receive a wiki-link."""
        now = datetime(2026, 4, 1)
        detector = ThreadDetector(now=now)
        fragments = [
            _make_fragment(
                "Garden Notes A",
                created=now - timedelta(days=10),
                primary_freq=Frequency.F1,
            ),
            _make_fragment(
                "Garden Notes B",
                created=now - timedelta(days=5),
                primary_freq=Frequency.F1,
            ),
            _make_fragment(
                "Garden Notes C",
                created=now - timedelta(days=1),
                primary_freq=Frequency.F1,
            ),
        ]
        threads = detector.detect_threads(fragments)
        updated = detector.assign_fragments_to_threads(fragments, threads)
        assert len(threads) == 1
        wikilink = f"[[{threads[0].title}]]"
        for frag in updated:
            assert wikilink in frag.threads

    def test_assignment_updates_thread_fragment_count(self) -> None:
        """assign_fragments_to_threads refreshes each thread's fragment_count."""
        now = datetime(2026, 4, 1)
        detector = ThreadDetector(now=now)
        fragments = [
            _make_fragment(
                f"Pebbles {i}",
                created=now - timedelta(days=10 - i),
                primary_freq=Frequency.F3,
            )
            for i in range(3)
        ]
        threads = detector.detect_threads(fragments)
        # Mutate count to ensure it's reset
        threads[0].fragment_count = 0
        detector.assign_fragments_to_threads(fragments, threads)
        assert threads[0].fragment_count == 3

    def test_assignment_skips_unassigned_fragments(self) -> None:
        """Fragments not in any thread are returned unchanged."""
        now = datetime(2026, 4, 1)
        detector = ThreadDetector(now=now)
        cluster = [
            _make_fragment(
                f"Topic {i}",
                created=now - timedelta(days=10 - i),
                primary_freq=Frequency.F1,
            )
            for i in range(3)
        ]
        loner = _make_fragment(
            "Loner",
            created=now - timedelta(days=2),
            primary_freq=Frequency.F9,
        )
        fragments = [*cluster, loner]
        threads = detector.detect_threads(fragments)
        updated = detector.assign_fragments_to_threads(fragments, threads)
        loner_updated = next(f for f in updated if f.id == loner.id)
        assert loner_updated is loner
        assert loner_updated.threads == []

    def test_assignment_does_not_duplicate_existing_links(self) -> None:
        """Pre-existing wiki-links are not duplicated."""
        now = datetime(2026, 4, 1)
        detector = ThreadDetector(now=now)
        fragments = [
            _make_fragment(
                f"Tea Ceremony {i}",
                created=now - timedelta(days=10 - i),
                primary_freq=Frequency.F4,
            )
            for i in range(3)
        ]
        threads = detector.detect_threads(fragments)
        wikilink = f"[[{threads[0].title}]]"
        # Pre-add the wikilink to one fragment
        fragments[0] = fragments[0].model_copy(update={"threads": [wikilink]})
        detector._thread_members[threads[0].id] = [f.id for f in fragments]
        updated = detector.assign_fragments_to_threads(fragments, threads)
        assert updated[0].threads.count(wikilink) == 1

    def test_assignment_returns_new_fragment_instances(self) -> None:
        """Assigned fragments are returned as fresh copies (no mutation)."""
        now = datetime(2026, 4, 1)
        detector = ThreadDetector(now=now)
        fragments = [
            _make_fragment(
                f"Sourdough {i}",
                created=now - timedelta(days=10 - i),
                primary_freq=Frequency.F5,
            )
            for i in range(3)
        ]
        threads = detector.detect_threads(fragments)
        updated = detector.assign_fragments_to_threads(fragments, threads)
        for original, new in zip(fragments, updated, strict=True):
            assert original.threads == []
            assert new.threads != []


class TestThreadMergeSuggestions:
    """Tests for the suggest_merges heuristic."""

    def test_suggest_merges_flags_overlapping_titles(self) -> None:
        """Threads with shared title words and frequencies are flagged."""
        detector = ThreadDetector()
        thread_a = Thread(
            title="Garden Project",
            frequency_affinity=[Frequency.F2],
        )
        thread_b = Thread(
            title="Garden Updates",
            frequency_affinity=[Frequency.F2],
        )
        suggestions = detector.suggest_merges([thread_a, thread_b])
        assert len(suggestions) == 1
        assert suggestions[0] == (thread_a, thread_b)

    def test_suggest_merges_ignores_disjoint_titles(self) -> None:
        """Threads with no shared content words are not suggested."""
        detector = ThreadDetector()
        thread_a = Thread(
            title="Apple Pie Recipe",
            frequency_affinity=[Frequency.F1],
        )
        thread_b = Thread(
            title="Mountain Hike",
            frequency_affinity=[Frequency.F1],
        )
        suggestions = detector.suggest_merges([thread_a, thread_b])
        assert suggestions == []

    def test_suggest_merges_requires_frequency_overlap(self) -> None:
        """Identical titles with disjoint frequency affinities don't merge."""
        detector = ThreadDetector()
        thread_a = Thread(
            title="Cooking Notes",
            frequency_affinity=[Frequency.F1],
        )
        thread_b = Thread(
            title="Cooking Notes",
            frequency_affinity=[Frequency.F9],
        )
        suggestions = detector.suggest_merges([thread_a, thread_b])
        assert suggestions == []

    def test_suggest_merges_empty_input(self) -> None:
        """Empty thread list returns no suggestions."""
        detector = ThreadDetector()
        assert detector.suggest_merges([]) == []

    def test_suggest_merges_skips_titles_without_content_words(self) -> None:
        """Single-letter titles produce no merges."""
        detector = ThreadDetector()
        thread_a = Thread(title="X", frequency_affinity=[Frequency.F1])
        thread_b = Thread(title="X", frequency_affinity=[Frequency.F1])
        assert detector.suggest_merges([thread_a, thread_b]) == []


# ---- EddyDetector Tests ----


class TestEddyDetector:
    """Tests for the EddyDetector stub class."""

    def test_detect_eddies_returns_empty_list(self) -> None:
        """Stub detect_eddies should return an empty list."""
        detector = EddyDetector()
        fragments = [_make_fragment("A"), _make_fragment("B")]
        result = detector.detect_eddies(fragments)
        assert result == []
        assert isinstance(result, list)

    def test_detect_eddies_empty_input(self) -> None:
        """detect_eddies with empty list should return empty list."""
        detector = EddyDetector()
        result = detector.detect_eddies([])
        assert result == []

    def test_detect_eddies_logs_message(self, caplog) -> None:
        """detect_eddies should log an info message."""
        detector = EddyDetector()
        fragments = [_make_fragment("A")]
        with caplog.at_level(logging.INFO, logger="creek.link.eddies"):
            detector.detect_eddies(fragments)
        assert any("edd" in r.message.lower() for r in caplog.records)


# ---- LinkingResult Tests ----


class TestLinkingResult:
    """Tests for the LinkingResult Pydantic model."""

    def test_creation_with_all_fields(self) -> None:
        """LinkingResult should accept all four count fields."""
        result = LinkingResult(
            resonance_count=5,
            temporal_count=3,
            thread_count=2,
            eddy_count=1,
        )
        assert result.resonance_count == 5
        assert result.temporal_count == 3
        assert result.thread_count == 2
        assert result.eddy_count == 1

    def test_zero_counts(self) -> None:
        """LinkingResult should work with all zero counts."""
        result = LinkingResult(
            resonance_count=0,
            temporal_count=0,
            thread_count=0,
            eddy_count=0,
        )
        assert result.resonance_count == 0
        assert result.temporal_count == 0
        assert result.thread_count == 0
        assert result.eddy_count == 0

    def test_model_dump(self) -> None:
        """LinkingResult model_dump should produce a serializable dict."""
        result = LinkingResult(
            resonance_count=1,
            temporal_count=2,
            thread_count=3,
            eddy_count=4,
        )
        dump = result.model_dump()
        assert dump == {
            "resonance_count": 1,
            "temporal_count": 2,
            "thread_count": 3,
            "eddy_count": 4,
        }


# ---- LinkingPipeline Tests ----


class TestLinkingPipeline:
    """Tests for the LinkingPipeline orchestrator class."""

    def test_init_stores_configs(self) -> None:
        """LinkingPipeline should store both config objects."""
        emb_config = EmbeddingsConfig()
        link_config = LinkingConfig()
        pipeline = LinkingPipeline(config=emb_config, linking_config=link_config)
        assert pipeline.config is emb_config
        assert pipeline.linking_config is link_config

    def test_run_returns_linking_result(self) -> None:
        """Pipeline.run should return a LinkingResult instance."""
        pipeline = LinkingPipeline(
            config=EmbeddingsConfig(),
            linking_config=LinkingConfig(),
        )
        fragments = [_make_fragment("A"), _make_fragment("B")]
        result = pipeline.run(
            fragments=fragments,
            vault_path=Path("/fake/vault"),
        )
        assert isinstance(result, LinkingResult)

    def test_run_returns_zero_temporal_thread_eddy(self) -> None:
        """Pipeline.run should return zero counts for still-stubbed linkers."""
        pipeline = LinkingPipeline(
            config=EmbeddingsConfig(),
            linking_config=LinkingConfig(),
        )
        fragments = [_make_fragment("A")]
        result = pipeline.run(
            fragments=fragments,
            vault_path=Path("/fake/vault"),
        )
        assert result.temporal_count == 0
        assert result.thread_count == 0
        assert result.eddy_count == 0

    def test_run_empty_fragments(self) -> None:
        """Pipeline.run with empty fragment list should succeed."""
        pipeline = LinkingPipeline(
            config=EmbeddingsConfig(),
            linking_config=LinkingConfig(),
        )
        result = pipeline.run(
            fragments=[],
            vault_path=Path("/fake/vault"),
        )
        assert isinstance(result, LinkingResult)
        assert result.resonance_count == 0

    def test_run_logs_pipeline_stages(self, caplog) -> None:
        """Pipeline.run should log info about each stage."""
        pipeline = LinkingPipeline(
            config=EmbeddingsConfig(),
            linking_config=LinkingConfig(),
        )
        fragments = [_make_fragment("A")]
        with caplog.at_level(logging.INFO):
            pipeline.run(
                fragments=fragments,
                vault_path=Path("/fake/vault"),
            )
        messages = " ".join(r.message.lower() for r in caplog.records)
        assert "embedding" in messages
        assert "temporal" in messages
        assert "thread" in messages
        assert "edd" in messages

    def test_add_wikilinks_to_threads(self) -> None:
        """add_wikilinks should add links to fragment threads list."""
        pipeline = LinkingPipeline(
            config=EmbeddingsConfig(),
            linking_config=LinkingConfig(),
        )
        fragment = _make_fragment("Test")
        assert fragment.threads == []
        updated = pipeline.add_wikilinks(
            fragment=fragment,
            links=["[[Thread A]]", "[[Thread B]]"],
        )
        assert "[[Thread A]]" in updated.threads
        assert "[[Thread B]]" in updated.threads

    def test_add_wikilinks_preserves_existing(self) -> None:
        """add_wikilinks should preserve existing thread entries."""
        pipeline = LinkingPipeline(
            config=EmbeddingsConfig(),
            linking_config=LinkingConfig(),
        )
        fragment = Fragment(
            title="Test",
            source=FragmentSource(platform=SourcePlatform.CLAUDE),
            threads=["existing-thread"],
        )
        updated = pipeline.add_wikilinks(
            fragment=fragment,
            links=["[[New Link]]"],
        )
        assert "existing-thread" in updated.threads
        assert "[[New Link]]" in updated.threads

    def test_add_wikilinks_empty_links(self) -> None:
        """add_wikilinks with empty links list should return fragment unchanged."""
        pipeline = LinkingPipeline(
            config=EmbeddingsConfig(),
            linking_config=LinkingConfig(),
        )
        fragment = _make_fragment("Test")
        updated = pipeline.add_wikilinks(fragment=fragment, links=[])
        assert updated.threads == fragment.threads

    def test_add_wikilinks_no_duplicates(self) -> None:
        """add_wikilinks should not add duplicate links."""
        pipeline = LinkingPipeline(
            config=EmbeddingsConfig(),
            linking_config=LinkingConfig(),
        )
        fragment = Fragment(
            title="Test",
            source=FragmentSource(platform=SourcePlatform.CLAUDE),
            threads=["[[Existing]]"],
        )
        updated = pipeline.add_wikilinks(
            fragment=fragment,
            links=["[[Existing]]", "[[New]]"],
        )
        assert updated.threads.count("[[Existing]]") == 1
        assert "[[New]]" in updated.threads

    def test_add_wikilinks_returns_new_fragment(self) -> None:
        """add_wikilinks should return a new Fragment, not mutate the original."""
        pipeline = LinkingPipeline(
            config=EmbeddingsConfig(),
            linking_config=LinkingConfig(),
        )
        fragment = _make_fragment("Test")
        updated = pipeline.add_wikilinks(
            fragment=fragment,
            links=["[[Link]]"],
        )
        assert fragment.threads == []
        assert "[[Link]]" in updated.threads
