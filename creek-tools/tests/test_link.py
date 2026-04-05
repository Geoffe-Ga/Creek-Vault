"""Tests for creek.link module — linking pipeline implementation.

Tests cover EmbeddingLinker, TemporalLinker, ThreadDetector, EddyDetector,
LinkingResult, and LinkingPipeline orchestration with real implementations.
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
    TemporalLinker,
    ThreadDetector,
)
from creek.link.eddies import EddyDetector as EddyDetectorDirect
from creek.link.embeddings import (
    EmbeddingLinker as EmbeddingLinkerDirect,
)
from creek.link.embeddings import (
    _cosine_similarity,
    _fragment_text,
    _tokenise,
)
from creek.link.linker import LinkingPipeline as LinkingPipelineDirect
from creek.link.linker import LinkingResult as LinkingResultDirect
from creek.link.temporal import TemporalLinker as TemporalLinkerDirect
from creek.link.threads import ThreadDetector as ThreadDetectorDirect
from creek.models import (
    Fragment,
    FragmentSource,
    Frequency,
    FrequencyClassification,
    SourcePlatform,
)


def _make_fragment(
    title: str = "Test Fragment",
    tags: list[str] | None = None,
    emotional_texture: list[str] | None = None,
    created: datetime | None = None,
    primary_freq: str = "unclassified",
    threads: list[str] | None = None,
) -> Fragment:
    """Create a Fragment for testing with configurable fields."""
    return Fragment(
        title=title,
        source=FragmentSource(platform=SourcePlatform.CLAUDE),
        tags=tags or [],
        emotional_texture=emotional_texture or [],
        created=created or datetime.now(),
        frequency=FrequencyClassification(primary=primary_freq),
        threads=threads or [],
    )


# ---- Package __init__ re-exports ----


class TestPackageExports:
    """Tests that creek.link.__init__ re-exports all public classes."""

    def test_embedding_linker_reexported(self) -> None:
        """EmbeddingLinker should be importable from creek.link."""
        assert EmbeddingLinker is EmbeddingLinkerDirect

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


# ---- Helper function tests ----


class TestTokenise:
    """Tests for the _tokenise helper function."""

    def test_basic_tokenisation(self) -> None:
        """Should split text into lowercase tokens."""
        assert _tokenise("Hello World") == ["hello", "world"]

    def test_strips_punctuation(self) -> None:
        """Should remove punctuation and keep alphanumeric tokens."""
        assert _tokenise("hello, world! foo-bar") == [
            "hello",
            "world",
            "foo",
            "bar",
        ]

    def test_empty_string(self) -> None:
        """Should return empty list for empty string."""
        assert _tokenise("") == []

    def test_numbers_included(self) -> None:
        """Should include numeric tokens."""
        assert _tokenise("test 123 abc") == ["test", "123", "abc"]


class TestFragmentText:
    """Tests for the _fragment_text helper function."""

    def test_title_only(self) -> None:
        """Should return title when no tags or emotional_texture."""
        frag = _make_fragment(title="My Title")
        assert _fragment_text(frag) == "My Title"

    def test_includes_tags(self) -> None:
        """Should include tags in the output text."""
        frag = _make_fragment(title="Title", tags=["python", "code"])
        text = _fragment_text(frag)
        assert "python" in text
        assert "code" in text

    def test_includes_emotional_texture(self) -> None:
        """Should include emotional_texture in the output text."""
        frag = _make_fragment(title="Title", emotional_texture=["joy", "wonder"])
        text = _fragment_text(frag)
        assert "joy" in text
        assert "wonder" in text


class TestCosineSimilarity:
    """Tests for the _cosine_similarity helper function."""

    def test_identical_vectors(self) -> None:
        """Identical vectors should have similarity 1.0."""
        vec = {"a": 1.0, "b": 2.0}
        assert abs(_cosine_similarity(vec, vec) - 1.0) < 1e-9

    def test_orthogonal_vectors(self) -> None:
        """Disjoint vectors should have similarity 0.0."""
        vec_a = {"a": 1.0}
        vec_b = {"b": 1.0}
        assert _cosine_similarity(vec_a, vec_b) == 0.0

    def test_empty_vector(self) -> None:
        """Empty vector should yield similarity 0.0."""
        assert _cosine_similarity({}, {"a": 1.0}) == 0.0
        assert _cosine_similarity({"a": 1.0}, {}) == 0.0

    def test_both_empty(self) -> None:
        """Two empty vectors should yield similarity 0.0."""
        assert _cosine_similarity({}, {}) == 0.0

    def test_zero_magnitude_vector(self) -> None:
        """Zero-magnitude vector with common keys should yield 0.0."""
        assert _cosine_similarity({"a": 0.0}, {"a": 1.0}) == 0.0

    def test_partial_overlap(self) -> None:
        """Partially overlapping vectors should have 0 < sim < 1."""
        vec_a = {"a": 1.0, "b": 1.0}
        vec_b = {"a": 1.0, "c": 1.0}
        sim = _cosine_similarity(vec_a, vec_b)
        assert 0.0 < sim < 1.0


# ---- EmbeddingLinker Tests ----


class TestEmbeddingLinker:
    """Tests for the EmbeddingLinker class."""

    def test_init_stores_config(self) -> None:
        """EmbeddingLinker should store the provided EmbeddingsConfig."""
        config = EmbeddingsConfig(model="test-model", similarity_threshold=0.8)
        linker = EmbeddingLinker(config=config)
        assert linker.config is config

    def test_generate_embeddings_returns_vectors(self) -> None:
        """Should return non-empty vectors for fragments with text."""
        config = EmbeddingsConfig()
        linker = EmbeddingLinker(config=config)
        fragments = [
            _make_fragment("Hello world", tags=["python"]),
            _make_fragment("Goodbye moon", tags=["rust"]),
        ]
        result = linker.generate_embeddings(fragments)
        assert len(result) == 2
        for frag in fragments:
            assert frag.id in result
            assert len(result[frag.id]) > 0

    def test_generate_embeddings_empty_input(self) -> None:
        """generate_embeddings with empty list should return empty dict."""
        config = EmbeddingsConfig()
        linker = EmbeddingLinker(config=config)
        result = linker.generate_embeddings([])
        assert result == {}

    def test_generate_embeddings_token_counts(self) -> None:
        """Embeddings should contain correct token frequency counts."""
        config = EmbeddingsConfig()
        linker = EmbeddingLinker(config=config)
        frag = _make_fragment("hello hello world")
        result = linker.generate_embeddings([frag])
        vec = result[frag.id]
        assert vec["hello"] == 2.0
        assert vec["world"] == 1.0

    def test_find_resonances_similar_fragments(self) -> None:
        """Should find resonances between fragments with similar text."""
        config = EmbeddingsConfig(similarity_threshold=0.5)
        linker = EmbeddingLinker(config=config)
        embeddings = {
            "frag-a": {"python": 2.0, "code": 1.0, "testing": 1.0},
            "frag-b": {"python": 1.0, "code": 2.0, "testing": 1.0},
            "frag-c": {"music": 2.0, "guitar": 1.0, "jazz": 1.0},
        }
        resonances = linker.find_resonances(embeddings)
        ids_in_resonances = {(r[0], r[1]) for r in resonances}
        assert ("frag-a", "frag-b") in ids_in_resonances
        # frag-c should not be linked to a or b
        assert ("frag-a", "frag-c") not in ids_in_resonances

    def test_find_resonances_returns_empty_below_threshold(self) -> None:
        """Should return empty when no pairs exceed threshold."""
        config = EmbeddingsConfig(similarity_threshold=0.99)
        linker = EmbeddingLinker(config=config)
        embeddings = {
            "frag-a": {"python": 1.0},
            "frag-b": {"rust": 1.0},
        }
        assert linker.find_resonances(embeddings) == []

    def test_find_resonances_empty_input(self) -> None:
        """find_resonances with empty dict should return empty list."""
        config = EmbeddingsConfig()
        linker = EmbeddingLinker(config=config)
        result = linker.find_resonances({})
        assert result == []

    def test_find_resonances_sorted_by_similarity(self) -> None:
        """Resonances should be sorted by similarity descending."""
        config = EmbeddingsConfig(similarity_threshold=0.0)
        linker = EmbeddingLinker(config=config)
        embeddings = {
            "a": {"x": 1.0, "y": 1.0},
            "b": {"x": 1.0, "y": 1.0},
            "c": {"x": 1.0, "z": 1.0},
        }
        resonances = linker.find_resonances(embeddings)
        sims = [r[2] for r in resonances]
        assert sims == sorted(sims, reverse=True)

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
            linker.find_resonances({"frag-1": {"a": 1.0}})
        assert any("resonance" in r.message.lower() for r in caplog.records)


# ---- TemporalLinker Tests ----


class TestTemporalLinker:
    """Tests for the TemporalLinker class."""

    def test_finds_links_within_window(self) -> None:
        """Should find pairs created within the time window."""
        now = datetime.now()
        linker = TemporalLinker()
        fragments = [
            _make_fragment("A", created=now),
            _make_fragment("B", created=now + timedelta(hours=2)),
        ]
        result = linker.find_temporal_links(fragments, window_hours=24)
        assert len(result) == 1

    def test_no_links_outside_window(self) -> None:
        """Should not link fragments outside the time window."""
        now = datetime.now()
        linker = TemporalLinker()
        fragments = [
            _make_fragment("A", created=now),
            _make_fragment("B", created=now + timedelta(hours=48)),
        ]
        result = linker.find_temporal_links(fragments, window_hours=24)
        assert result == []

    def test_empty_input(self) -> None:
        """Empty input should return empty list."""
        linker = TemporalLinker()
        result = linker.find_temporal_links([], window_hours=24)
        assert result == []

    def test_single_fragment(self) -> None:
        """Single fragment should return empty list."""
        linker = TemporalLinker()
        result = linker.find_temporal_links([_make_fragment("A")], window_hours=24)
        assert result == []

    def test_multiple_links(self) -> None:
        """Should find all pairs within window."""
        now = datetime.now()
        linker = TemporalLinker()
        fragments = [
            _make_fragment("A", created=now),
            _make_fragment("B", created=now + timedelta(hours=1)),
            _make_fragment("C", created=now + timedelta(hours=2)),
        ]
        result = linker.find_temporal_links(fragments, window_hours=24)
        assert len(result) == 3  # A-B, A-C, B-C

    def test_sorted_by_proximity(self) -> None:
        """Result should be sorted by time proximity (closest first)."""
        now = datetime.now()
        linker = TemporalLinker()
        frags = [
            _make_fragment("A", created=now),
            _make_fragment("B", created=now + timedelta(hours=10)),
            _make_fragment("C", created=now + timedelta(hours=1)),
        ]
        result = linker.find_temporal_links(frags, window_hours=24)
        # Closest pair should be first
        assert len(result) >= 2

    def test_custom_window(self) -> None:
        """Should respect custom window_hours parameter."""
        now = datetime.now()
        linker = TemporalLinker()
        fragments = [
            _make_fragment("A", created=now),
            _make_fragment("B", created=now + timedelta(hours=5)),
        ]
        assert linker.find_temporal_links(fragments, window_hours=4) == []
        assert len(linker.find_temporal_links(fragments, window_hours=6)) == 1

    def test_logs_message(self, caplog) -> None:
        """find_temporal_links should log an info message."""
        linker = TemporalLinker()
        fragments = [_make_fragment("A")]
        with caplog.at_level(logging.INFO, logger="creek.link.temporal"):
            linker.find_temporal_links(fragments, window_hours=168)
        assert any("temporal" in r.message.lower() for r in caplog.records)


# ---- ThreadDetector Tests ----


class TestThreadDetector:
    """Tests for the ThreadDetector class."""

    def test_detects_threads_by_frequency(self) -> None:
        """Should create threads for frequency groups meeting threshold."""
        config = LinkingConfig(thread_min_fragments=2)
        detector = ThreadDetector(config=config)
        fragments = [
            _make_fragment("A", primary_freq=Frequency.F1),
            _make_fragment("B", primary_freq=Frequency.F1),
            _make_fragment("C", primary_freq=Frequency.F2),
        ]
        threads = detector.detect_threads(fragments)
        assert len(threads) == 1
        assert threads[0].frequency_affinity == [Frequency.F1]
        assert threads[0].fragment_count == 2

    def test_ignores_unclassified(self) -> None:
        """Should not create threads for UNCLASSIFIED frequency."""
        config = LinkingConfig(thread_min_fragments=2)
        detector = ThreadDetector(config=config)
        fragments = [
            _make_fragment("A", primary_freq=Frequency.UNCLASSIFIED),
            _make_fragment("B", primary_freq=Frequency.UNCLASSIFIED),
        ]
        threads = detector.detect_threads(fragments)
        assert threads == []

    def test_below_threshold(self) -> None:
        """Should not create thread if below min_fragments."""
        config = LinkingConfig(thread_min_fragments=5)
        detector = ThreadDetector(config=config)
        fragments = [
            _make_fragment("A", primary_freq=Frequency.F1),
            _make_fragment("B", primary_freq=Frequency.F1),
        ]
        threads = detector.detect_threads(fragments)
        assert threads == []

    def test_empty_input(self) -> None:
        """Empty input should return empty list."""
        detector = ThreadDetector()
        assert detector.detect_threads([]) == []

    def test_thread_has_correct_dates(self) -> None:
        """Thread should have correct first_seen and last_seen dates."""
        config = LinkingConfig(thread_min_fragments=2)
        detector = ThreadDetector(config=config)
        d1 = datetime(2024, 1, 1)
        d2 = datetime(2024, 6, 15)
        fragments = [
            _make_fragment("A", primary_freq=Frequency.F3, created=d1),
            _make_fragment("B", primary_freq=Frequency.F3, created=d2),
        ]
        threads = detector.detect_threads(fragments)
        assert len(threads) == 1
        assert threads[0].first_seen == d1.date()
        assert threads[0].last_seen == d2.date()

    def test_multiple_threads(self) -> None:
        """Should detect multiple threads for different frequencies."""
        config = LinkingConfig(thread_min_fragments=2)
        detector = ThreadDetector(config=config)
        fragments = [
            _make_fragment("A1", primary_freq=Frequency.F1),
            _make_fragment("A2", primary_freq=Frequency.F1),
            _make_fragment("B1", primary_freq=Frequency.F5),
            _make_fragment("B2", primary_freq=Frequency.F5),
        ]
        threads = detector.detect_threads(fragments)
        assert len(threads) == 2

    def test_sorted_by_fragment_count(self) -> None:
        """Threads should be sorted by fragment count descending."""
        config = LinkingConfig(thread_min_fragments=2)
        detector = ThreadDetector(config=config)
        fragments = [
            _make_fragment("A1", primary_freq=Frequency.F1),
            _make_fragment("A2", primary_freq=Frequency.F1),
            _make_fragment("B1", primary_freq=Frequency.F5),
            _make_fragment("B2", primary_freq=Frequency.F5),
            _make_fragment("B3", primary_freq=Frequency.F5),
        ]
        threads = detector.detect_threads(fragments)
        counts = [t.fragment_count for t in threads]
        assert counts == sorted(counts, reverse=True)

    def test_default_config(self) -> None:
        """Should use default config when none provided."""
        detector = ThreadDetector()
        assert detector.config.thread_min_fragments == 3

    def test_logs_message(self, caplog) -> None:
        """detect_threads should log an info message."""
        detector = ThreadDetector()
        fragments = [_make_fragment("A")]
        with caplog.at_level(logging.INFO, logger="creek.link.threads"):
            detector.detect_threads(fragments)
        assert any("thread" in r.message.lower() for r in caplog.records)

    def test_thread_title_contains_label(self) -> None:
        """Thread title should contain the frequency label."""
        config = LinkingConfig(thread_min_fragments=2)
        detector = ThreadDetector(config=config)
        fragments = [
            _make_fragment("A", primary_freq=Frequency.F1),
            _make_fragment("B", primary_freq=Frequency.F1),
        ]
        threads = detector.detect_threads(fragments)
        assert "Survival" in threads[0].title


# ---- EddyDetector Tests ----


class TestEddyDetector:
    """Tests for the EddyDetector class."""

    def test_detects_eddies_by_shared_tags(self) -> None:
        """Should create eddies for tags meeting the threshold."""
        config = LinkingConfig(eddy_min_fragments=2)
        detector = EddyDetector(config=config)
        fragments = [
            _make_fragment("A", tags=["python", "code"]),
            _make_fragment("B", tags=["python", "testing"]),
            _make_fragment("C", tags=["music"]),
        ]
        eddies = detector.detect_eddies(fragments)
        assert len(eddies) == 1
        assert "python" in eddies[0].title
        assert eddies[0].fragment_count == 2

    def test_below_threshold(self) -> None:
        """Should not create eddy if below min_fragments."""
        config = LinkingConfig(eddy_min_fragments=5)
        detector = EddyDetector(config=config)
        fragments = [
            _make_fragment("A", tags=["python"]),
            _make_fragment("B", tags=["python"]),
        ]
        eddies = detector.detect_eddies(fragments)
        assert eddies == []

    def test_empty_input(self) -> None:
        """Empty input should return empty list."""
        detector = EddyDetector()
        assert detector.detect_eddies([]) == []

    def test_no_tags(self) -> None:
        """Fragments without tags should produce no eddies."""
        config = LinkingConfig(eddy_min_fragments=2)
        detector = EddyDetector(config=config)
        fragments = [_make_fragment("A"), _make_fragment("B")]
        assert detector.detect_eddies(fragments) == []

    def test_eddy_collects_thread_ids(self) -> None:
        """Eddy should collect thread IDs from member fragments."""
        config = LinkingConfig(eddy_min_fragments=2)
        detector = EddyDetector(config=config)
        fragments = [
            _make_fragment("A", tags=["python"], threads=["thread-1"]),
            _make_fragment("B", tags=["python"], threads=["thread-2"]),
        ]
        eddies = detector.detect_eddies(fragments)
        assert len(eddies) == 1
        assert "thread-1" in eddies[0].threads
        assert "thread-2" in eddies[0].threads

    def test_eddy_no_duplicate_thread_ids(self) -> None:
        """Eddy should not contain duplicate thread IDs."""
        config = LinkingConfig(eddy_min_fragments=2)
        detector = EddyDetector(config=config)
        fragments = [
            _make_fragment("A", tags=["python"], threads=["thread-1"]),
            _make_fragment("B", tags=["python"], threads=["thread-1"]),
        ]
        eddies = detector.detect_eddies(fragments)
        assert eddies[0].threads.count("thread-1") == 1

    def test_multiple_eddies(self) -> None:
        """Should detect multiple eddies for different tags."""
        config = LinkingConfig(eddy_min_fragments=2)
        detector = EddyDetector(config=config)
        fragments = [
            _make_fragment("A", tags=["python", "music"]),
            _make_fragment("B", tags=["python", "music"]),
        ]
        eddies = detector.detect_eddies(fragments)
        assert len(eddies) == 2

    def test_sorted_by_fragment_count(self) -> None:
        """Eddies should be sorted by fragment count descending."""
        config = LinkingConfig(eddy_min_fragments=2)
        detector = EddyDetector(config=config)
        fragments = [
            _make_fragment("A", tags=["python", "code"]),
            _make_fragment("B", tags=["python", "code"]),
            _make_fragment("C", tags=["python"]),
        ]
        eddies = detector.detect_eddies(fragments)
        counts = [e.fragment_count for e in eddies]
        assert counts == sorted(counts, reverse=True)

    def test_default_config(self) -> None:
        """Should use default config when none provided."""
        detector = EddyDetector()
        assert detector.config.eddy_min_fragments == 5

    def test_logs_message(self, caplog) -> None:
        """detect_eddies should log an info message."""
        detector = EddyDetector()
        fragments = [_make_fragment("A")]
        with caplog.at_level(logging.INFO, logger="creek.link.eddies"):
            detector.detect_eddies(fragments)
        assert any("edd" in r.message.lower() for r in caplog.records)

    def test_eddy_has_formed_date(self) -> None:
        """Eddy formed date should be the earliest fragment date."""
        config = LinkingConfig(eddy_min_fragments=2)
        detector = EddyDetector(config=config)
        d1 = datetime(2024, 1, 1)
        d2 = datetime(2024, 6, 15)
        fragments = [
            _make_fragment("A", tags=["python"], created=d1),
            _make_fragment("B", tags=["python"], created=d2),
        ]
        eddies = detector.detect_eddies(fragments)
        assert eddies[0].formed == d1.date()


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

    def test_run_produces_real_counts(self) -> None:
        """Pipeline.run with related fragments should produce counts."""
        now = datetime.now()
        pipeline = LinkingPipeline(
            config=EmbeddingsConfig(similarity_threshold=0.5),
            linking_config=LinkingConfig(
                thread_min_fragments=2,
                eddy_min_fragments=2,
                temporal_window_hours=24,
            ),
        )
        fragments = [
            _make_fragment(
                "python coding tutorial",
                tags=["python"],
                primary_freq=Frequency.F5,
                created=now,
            ),
            _make_fragment(
                "python testing guide",
                tags=["python"],
                primary_freq=Frequency.F5,
                created=now + timedelta(hours=1),
            ),
        ]
        result = pipeline.run(
            fragments=fragments,
            vault_path=Path("/fake/vault"),
        )
        assert result.temporal_count >= 1
        assert result.thread_count >= 1
        assert result.eddy_count >= 1

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
        fragment = _make_fragment("Test", threads=["existing-thread"])
        updated = pipeline.add_wikilinks(
            fragment=fragment,
            links=["[[New Link]]"],
        )
        assert "existing-thread" in updated.threads
        assert "[[New Link]]" in updated.threads

    def test_add_wikilinks_empty_links(self) -> None:
        """add_wikilinks with empty links list returns fragment unchanged."""
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
        fragment = _make_fragment("Test", threads=["[[Existing]]"])
        updated = pipeline.add_wikilinks(
            fragment=fragment,
            links=["[[Existing]]", "[[New]]"],
        )
        assert updated.threads.count("[[Existing]]") == 1
        assert "[[New]]" in updated.threads

    def test_add_wikilinks_returns_new_fragment(self) -> None:
        """add_wikilinks should return new Fragment, not mutate original."""
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
