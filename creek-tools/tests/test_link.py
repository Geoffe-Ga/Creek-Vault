"""Tests for creek.link module — linking pipeline and component linkers.

Tests cover EmbeddingLinker, TemporalLinker, ThreadDetector, EddyDetector,
LinkingResult, and LinkingPipeline orchestration.
"""

import logging
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import numpy as np

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
from creek.link.embeddings import EmbeddingLinker as EmbeddingLinkerDirect
from creek.link.linker import LinkingPipeline as LinkingPipelineDirect
from creek.link.linker import LinkingResult as LinkingResultDirect
from creek.link.temporal import TemporalLinker as TemporalLinkerDirect
from creek.link.threads import ThreadDetector as ThreadDetectorDirect
from creek.models import Fragment, FragmentSource, SourcePlatform

_DIMS = 384


def _make_fragment(title: str = "Test Fragment") -> Fragment:
    """Create a minimal Fragment for testing."""
    return Fragment(
        title=title,
        source=FragmentSource(platform=SourcePlatform.CLAUDE),
    )


def _make_mock_model(dims: int = _DIMS) -> MagicMock:
    """Create a mock SentenceTransformer that returns deterministic embeddings."""
    model = MagicMock()

    def _encode(
        sentences: str | list[str],
        show_progress_bar: bool = False,
        batch_size: int = 32,
        **kwargs: Any,
    ) -> np.ndarray:
        if isinstance(sentences, str):
            rng = np.random.default_rng(hash(sentences) % 2**32)
            return rng.standard_normal(dims).astype(np.float32)
        rng_batch = [
            np.random.default_rng(hash(s) % 2**32).standard_normal(dims)
            for s in sentences
        ]
        return np.array(rng_batch, dtype=np.float32)

    model.encode = MagicMock(side_effect=_encode)
    return model


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


# ---- EmbeddingLinker Tests ----


class TestEmbeddingLinker:
    """Tests for the EmbeddingLinker class."""

    def test_init_stores_config(self) -> None:
        """EmbeddingLinker should store the provided EmbeddingsConfig."""
        config = EmbeddingsConfig(model="test-model", similarity_threshold=0.8)
        linker = EmbeddingLinker(config=config)
        assert linker.config is config

    @patch("creek.link.embeddings._load_sentence_transformer")
    def test_generate_embeddings_returns_dict(self, mock_load) -> None:
        """generate_embeddings should return a dict keyed by fragment IDs."""
        mock_load.return_value = _make_mock_model()
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

    @patch("creek.link.embeddings._load_sentence_transformer")
    def test_generate_embeddings_logs_message(self, mock_load, caplog) -> None:
        """generate_embeddings should log an info message."""
        mock_load.return_value = _make_mock_model()
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


# ---- TemporalLinker Tests ----


class TestTemporalLinker:
    """Tests for the TemporalLinker stub class."""

    def test_find_temporal_links_returns_empty_list(self) -> None:
        """Stub find_temporal_links should return an empty list."""
        linker = TemporalLinker()
        fragments = [_make_fragment("A"), _make_fragment("B")]
        result = linker.find_temporal_links(fragments, window_hours=168)
        assert result == []
        assert isinstance(result, list)

    def test_find_temporal_links_empty_input(self) -> None:
        """find_temporal_links with empty list should return empty list."""
        linker = TemporalLinker()
        result = linker.find_temporal_links([], window_hours=24)
        assert result == []

    def test_find_temporal_links_custom_window(self) -> None:
        """find_temporal_links should accept custom window_hours."""
        linker = TemporalLinker()
        fragments = [_make_fragment("A")]
        result = linker.find_temporal_links(fragments, window_hours=48)
        assert result == []

    def test_find_temporal_links_logs_message(self, caplog) -> None:
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

    @patch("creek.link.embeddings._load_sentence_transformer")
    def test_run_returns_linking_result(self, mock_load) -> None:
        """Pipeline.run should return a LinkingResult instance."""
        mock_load.return_value = _make_mock_model()
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

    @patch("creek.link.embeddings._load_sentence_transformer")
    def test_run_returns_zero_temporal_thread_eddy(self, mock_load) -> None:
        """Pipeline.run should return zero counts for still-stubbed linkers."""
        mock_load.return_value = _make_mock_model()
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

    @patch("creek.link.embeddings._load_sentence_transformer")
    def test_run_logs_pipeline_stages(self, mock_load, caplog) -> None:
        """Pipeline.run should log info about each stage."""
        mock_load.return_value = _make_mock_model()
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
