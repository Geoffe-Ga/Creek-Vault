"""Tests for creek.link.embeddings — EmbeddingLinker implementation.

Tests cover model loading, single/batch embedding generation,
disk persistence (save/load), incremental mode, and resonance finding.
All tests mock the SentenceTransformer to avoid model downloads.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock, patch

if TYPE_CHECKING:
    from pathlib import Path

import numpy as np
import pytest

from creek.config import EmbeddingsConfig
from creek.link.embeddings import EmbeddingLinker
from creek.models import Fragment, FragmentSource, SourcePlatform

_DIMS = 384  # all-MiniLM-L6-v2 output dimensions


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


# ---- Model Loading ----


class TestLoadModel:
    """Tests for the load_model method."""

    @patch("creek.link.embeddings._load_sentence_transformer")
    def test_load_model_creates_instance(self, mock_load: MagicMock) -> None:
        """load_model should instantiate SentenceTransformer with config."""
        mock_load.return_value = _make_mock_model()
        config = EmbeddingsConfig(model="all-MiniLM-L6-v2")
        linker = EmbeddingLinker(config=config)
        result = linker.load_model()
        mock_load.assert_called_once_with(
            "all-MiniLM-L6-v2",
            None,
        )
        assert result is mock_load.return_value

    @patch("creek.link.embeddings._load_sentence_transformer")
    def test_load_model_caches_instance(self, mock_load: MagicMock) -> None:
        """Calling load_model twice should only create one instance."""
        mock_load.return_value = _make_mock_model()
        linker = EmbeddingLinker(config=EmbeddingsConfig())
        first = linker.load_model()
        second = linker.load_model()
        assert first is second
        assert mock_load.call_count == 1

    @patch("creek.link.embeddings._load_sentence_transformer")
    def test_load_model_uses_cache_dir(self, mock_load: MagicMock) -> None:
        """load_model should pass cache_dir from config."""
        mock_load.return_value = _make_mock_model()
        config = EmbeddingsConfig(cache_dir="/tmp/models")
        linker = EmbeddingLinker(config=config)
        linker.load_model()
        mock_load.assert_called_once_with(
            "all-MiniLM-L6-v2",
            "/tmp/models",
        )


# ---- Single Embedding ----


class TestGenerateEmbedding:
    """Tests for the generate_embedding method (single text)."""

    @patch("creek.link.embeddings._load_sentence_transformer")
    def test_returns_float_list(self, mock_load: MagicMock) -> None:
        """generate_embedding should return a list of floats."""
        mock_load.return_value = _make_mock_model()
        linker = EmbeddingLinker(config=EmbeddingsConfig())
        result = linker.generate_embedding("hello world")
        assert isinstance(result, list)
        assert all(isinstance(v, float) for v in result)

    @patch("creek.link.embeddings._load_sentence_transformer")
    def test_correct_dimensions(self, mock_load: MagicMock) -> None:
        """generate_embedding should return vector with correct dimensions."""
        mock_load.return_value = _make_mock_model(dims=_DIMS)
        linker = EmbeddingLinker(config=EmbeddingsConfig())
        result = linker.generate_embedding("hello world")
        assert len(result) == _DIMS

    @patch("creek.link.embeddings._load_sentence_transformer")
    def test_consistent_results(self, mock_load: MagicMock) -> None:
        """Same text should produce the same embedding."""
        mock_load.return_value = _make_mock_model()
        linker = EmbeddingLinker(config=EmbeddingsConfig())
        first = linker.generate_embedding("hello")
        second = linker.generate_embedding("hello")
        assert first == second


# ---- Batch Embedding ----


class TestGenerateEmbeddings:
    """Tests for the generate_embeddings method (batch fragments)."""

    @patch("creek.link.embeddings._load_sentence_transformer")
    def test_returns_dict_with_fragment_ids(self, mock_load: MagicMock) -> None:
        """generate_embeddings should return dict keyed by fragment IDs."""
        mock_load.return_value = _make_mock_model()
        linker = EmbeddingLinker(config=EmbeddingsConfig())
        frags = [_make_fragment("A"), _make_fragment("B")]
        result = linker.generate_embeddings(frags)
        assert isinstance(result, dict)
        assert set(result.keys()) == {f.id for f in frags}

    @patch("creek.link.embeddings._load_sentence_transformer")
    def test_empty_input_returns_empty_dict(self, mock_load: MagicMock) -> None:
        """generate_embeddings with empty list should return empty dict."""
        mock_load.return_value = _make_mock_model()
        linker = EmbeddingLinker(config=EmbeddingsConfig())
        result = linker.generate_embeddings([])
        assert result == {}

    @patch("creek.link.embeddings._load_sentence_transformer")
    def test_embeddings_have_correct_dimensions(self, mock_load: MagicMock) -> None:
        """Each embedding vector should have the correct number of dims."""
        mock_load.return_value = _make_mock_model(dims=_DIMS)
        linker = EmbeddingLinker(config=EmbeddingsConfig())
        frags = [_make_fragment("Test")]
        result = linker.generate_embeddings(frags)
        for vec in result.values():
            assert len(vec) == _DIMS

    @patch("creek.link.embeddings._load_sentence_transformer")
    def test_uses_fragment_title_for_text(self, mock_load: MagicMock) -> None:
        """generate_embeddings should encode fragment titles."""
        mock_model = _make_mock_model()
        mock_load.return_value = mock_model
        linker = EmbeddingLinker(config=EmbeddingsConfig())
        frags = [_make_fragment("Alpha"), _make_fragment("Beta")]
        linker.generate_embeddings(frags)
        call_args = mock_model.encode.call_args
        texts = call_args[0][0]
        assert "Alpha" in texts[0]
        assert "Beta" in texts[1]

    @patch("creek.link.embeddings._load_sentence_transformer")
    def test_passes_batch_size_from_config(self, mock_load: MagicMock) -> None:
        """generate_embeddings should pass batch_size to model.encode."""
        mock_model = _make_mock_model()
        mock_load.return_value = mock_model
        config = EmbeddingsConfig(batch_size=16)
        linker = EmbeddingLinker(config=config)
        frags = [_make_fragment("A")]
        linker.generate_embeddings(frags)
        call_kwargs = mock_model.encode.call_args[1]
        assert call_kwargs["batch_size"] == 16

    @patch("creek.link.embeddings._load_sentence_transformer")
    def test_shows_progress_bar(self, mock_load: MagicMock) -> None:
        """generate_embeddings should enable the progress bar."""
        mock_model = _make_mock_model()
        mock_load.return_value = mock_model
        linker = EmbeddingLinker(config=EmbeddingsConfig())
        frags = [_make_fragment("A")]
        linker.generate_embeddings(frags)
        call_kwargs = mock_model.encode.call_args[1]
        assert call_kwargs["show_progress_bar"] is True


# ---- Incremental Mode ----


class TestIncrementalMode:
    """Tests for incremental embedding generation."""

    @patch("creek.link.embeddings._load_sentence_transformer")
    def test_skips_existing_ids(self, mock_load: MagicMock) -> None:
        """generate_embeddings should skip fragments in existing_ids."""
        mock_model = _make_mock_model()
        mock_load.return_value = mock_model
        linker = EmbeddingLinker(config=EmbeddingsConfig())
        frag_a = _make_fragment("A")
        frag_b = _make_fragment("B")
        result = linker.generate_embeddings(
            [frag_a, frag_b],
            existing_ids={frag_a.id},
        )
        assert frag_a.id not in result
        assert frag_b.id in result

    @patch("creek.link.embeddings._load_sentence_transformer")
    def test_all_existing_returns_empty(self, mock_load: MagicMock) -> None:
        """If all fragments are in existing_ids, return empty dict."""
        mock_load.return_value = _make_mock_model()
        linker = EmbeddingLinker(config=EmbeddingsConfig())
        frag = _make_fragment("A")
        result = linker.generate_embeddings([frag], existing_ids={frag.id})
        assert result == {}

    @patch("creek.link.embeddings._load_sentence_transformer")
    def test_no_existing_ids_processes_all(self, mock_load: MagicMock) -> None:
        """Without existing_ids, all fragments should be processed."""
        mock_load.return_value = _make_mock_model()
        linker = EmbeddingLinker(config=EmbeddingsConfig())
        frags = [_make_fragment("A"), _make_fragment("B")]
        result = linker.generate_embeddings(frags)
        assert len(result) == 2


# ---- Save / Load ----


class TestSaveLoadEmbeddings:
    """Tests for save_embeddings and load_embeddings disk persistence."""

    def test_roundtrip(self, tmp_path: Path) -> None:
        """Saved embeddings should be loadable and identical."""
        linker = EmbeddingLinker(config=EmbeddingsConfig())
        original = {
            "frag-001": [0.1, 0.2, 0.3],
            "frag-002": [0.4, 0.5, 0.6],
        }
        save_path = tmp_path / "embeddings.npz"
        linker.save_embeddings(original, save_path)
        loaded = linker.load_embeddings(save_path)
        assert set(loaded.keys()) == set(original.keys())
        for key in original:
            np.testing.assert_allclose(loaded[key], original[key], rtol=1e-5)

    def test_save_creates_file(self, tmp_path: Path) -> None:
        """save_embeddings should create the .npz file."""
        linker = EmbeddingLinker(config=EmbeddingsConfig())
        save_path = tmp_path / "test.npz"
        linker.save_embeddings({"a": [1.0]}, save_path)
        assert save_path.exists()

    def test_load_missing_file_raises(self, tmp_path: Path) -> None:
        """load_embeddings should raise FileNotFoundError for missing file."""
        linker = EmbeddingLinker(config=EmbeddingsConfig())
        with pytest.raises(FileNotFoundError):
            linker.load_embeddings(tmp_path / "nonexistent.npz")

    def test_empty_embeddings_roundtrip(self, tmp_path: Path) -> None:
        """Saving and loading empty embeddings should work."""
        linker = EmbeddingLinker(config=EmbeddingsConfig())
        save_path = tmp_path / "empty.npz"
        linker.save_embeddings({}, save_path)
        loaded = linker.load_embeddings(save_path)
        assert loaded == {}


# ---- Find Resonances ----


class TestFindResonances:
    """Tests for the find_resonances cosine similarity method."""

    def test_identical_vectors_have_similarity_one(self) -> None:
        """Identical vectors should have cosine similarity of 1.0."""
        linker = EmbeddingLinker(
            config=EmbeddingsConfig(similarity_threshold=0.5),
        )
        embeddings = {
            "a": [1.0, 0.0, 0.0],
            "b": [1.0, 0.0, 0.0],
        }
        result = linker.find_resonances(embeddings)
        assert len(result) == 1
        assert result[0][0] == "a"
        assert result[0][1] == "b"
        assert abs(result[0][2] - 1.0) < 1e-6

    def test_orthogonal_vectors_no_resonance(self) -> None:
        """Orthogonal vectors should not produce resonances."""
        linker = EmbeddingLinker(
            config=EmbeddingsConfig(similarity_threshold=0.5),
        )
        embeddings = {
            "a": [1.0, 0.0, 0.0],
            "b": [0.0, 1.0, 0.0],
        }
        result = linker.find_resonances(embeddings)
        assert result == []

    def test_threshold_filtering(self) -> None:
        """Only pairs above the similarity threshold should be returned."""
        linker = EmbeddingLinker(
            config=EmbeddingsConfig(similarity_threshold=0.9),
        )
        embeddings = {
            "a": [1.0, 0.0],
            "b": [0.9, 0.436],  # cosine sim ~0.9
            "c": [0.0, 1.0],  # orthogonal to a
        }
        result = linker.find_resonances(embeddings)
        # a-b should be close to threshold, a-c and b-c should be below
        pair_ids = {(r[0], r[1]) for r in result}
        assert ("a", "c") not in pair_ids

    def test_empty_embeddings_returns_empty(self) -> None:
        """find_resonances with empty embeddings should return empty list."""
        linker = EmbeddingLinker(config=EmbeddingsConfig())
        result = linker.find_resonances({})
        assert result == []

    def test_single_embedding_returns_empty(self) -> None:
        """find_resonances with one embedding should return empty list."""
        linker = EmbeddingLinker(config=EmbeddingsConfig())
        result = linker.find_resonances({"a": [1.0, 0.0]})
        assert result == []

    def test_result_tuple_structure(self) -> None:
        """Each resonance should be a (id_a, id_b, similarity) tuple."""
        linker = EmbeddingLinker(
            config=EmbeddingsConfig(similarity_threshold=0.0),
        )
        embeddings = {
            "a": [1.0, 0.0],
            "b": [0.7, 0.7],
        }
        result = linker.find_resonances(embeddings)
        assert len(result) >= 1
        frag_a, frag_b, sim = result[0]
        assert isinstance(frag_a, str)
        assert isinstance(frag_b, str)
        assert isinstance(sim, float)
        assert 0.0 <= sim <= 1.0

    def test_no_duplicate_pairs(self) -> None:
        """Each pair should appear only once (no (b,a) if (a,b) exists)."""
        linker = EmbeddingLinker(
            config=EmbeddingsConfig(similarity_threshold=0.0),
        )
        embeddings = {
            "a": [1.0, 0.0],
            "b": [0.9, 0.1],
            "c": [0.8, 0.2],
        }
        result = linker.find_resonances(embeddings)
        pairs = [(r[0], r[1]) for r in result]
        # Check no reverse duplicates
        for id_a, id_b in pairs:
            assert (id_b, id_a) not in pairs


# ---- Logging ----


class TestEmbeddingLinkerLogging:
    """Tests for logging behaviour."""

    @patch("creek.link.embeddings._load_sentence_transformer")
    def test_generate_embeddings_logs_count(
        self, mock_load: MagicMock, caplog: pytest.LogCaptureFixture
    ) -> None:
        """generate_embeddings should log the number of fragments."""
        mock_load.return_value = _make_mock_model()
        linker = EmbeddingLinker(config=EmbeddingsConfig())
        frags = [_make_fragment("A"), _make_fragment("B")]
        import logging

        with caplog.at_level(logging.INFO, logger="creek.link.embeddings"):
            linker.generate_embeddings(frags)
        assert any("2" in r.message for r in caplog.records)

    def test_find_resonances_logs_count(self, caplog: pytest.LogCaptureFixture) -> None:
        """find_resonances should log the number of embeddings."""
        linker = EmbeddingLinker(config=EmbeddingsConfig())
        import logging

        with caplog.at_level(logging.INFO, logger="creek.link.embeddings"):
            linker.find_resonances({"a": [1.0], "b": [0.0]})
        assert any("2" in r.message for r in caplog.records)
