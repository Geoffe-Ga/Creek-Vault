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
    """Tests for the ThreadDetector stub class."""

    def test_detect_threads_returns_empty_list(self) -> None:
        """Stub detect_threads should return an empty list."""
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
