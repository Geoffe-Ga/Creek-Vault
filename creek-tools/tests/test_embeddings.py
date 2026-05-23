"""Tests for creek.link.embeddings — EmbeddingLinker implementation.

Tests cover model loading, single/batch embedding generation,
disk persistence (save/load), incremental mode, and resonance finding.
All tests use the autouse ``mock_sentence_transformer`` fixture from
conftest.py to avoid model downloads.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import numpy as np

from creek.config import EmbeddingsConfig
from creek.link.embeddings import (
    CachedEmbedding,
    EmbeddingLinker,
    content_hash_for_text,
    embeddings_cache_path,
    fragment_embedding_text,
)
from creek.models import Fragment, FragmentSource, SourcePlatform

if TYPE_CHECKING:
    from pathlib import Path
    from unittest.mock import MagicMock

    import pytest

_DIMS = 384  # all-MiniLM-L6-v2 output dimensions


def _make_fragment(title: str = "Test Fragment") -> Fragment:
    """Create a minimal Fragment for testing."""
    from creek.models import synthetic_fragment_id

    return Fragment(
        id=synthetic_fragment_id(),
        title=title,
        source=FragmentSource(platform=SourcePlatform.CLAUDE),
    )


# ---- Model Loading ----


class TestLoadModel:
    """Tests for the load_model method."""

    def test_load_model_creates_instance(
        self, mock_sentence_transformer: MagicMock
    ) -> None:
        """load_model should instantiate SentenceTransformer with config."""
        config = EmbeddingsConfig(model="all-MiniLM-L6-v2")
        linker = EmbeddingLinker(config=config)
        result = linker.load_model()
        mock_sentence_transformer.assert_called_once_with(
            "all-MiniLM-L6-v2",
            None,
        )
        assert result is mock_sentence_transformer.return_value

    def test_load_model_caches_instance(
        self, mock_sentence_transformer: MagicMock
    ) -> None:
        """Calling load_model twice should only create one instance."""
        linker = EmbeddingLinker(config=EmbeddingsConfig())
        first = linker.load_model()
        second = linker.load_model()
        assert first is second
        assert mock_sentence_transformer.call_count == 1

    def test_load_model_uses_cache_dir(
        self, mock_sentence_transformer: MagicMock, tmp_path: Path
    ) -> None:
        """load_model should pass cache_dir from config."""
        cache_dir = str(tmp_path / "models")
        config = EmbeddingsConfig(cache_dir=cache_dir)
        linker = EmbeddingLinker(config=config)
        linker.load_model()
        mock_sentence_transformer.assert_called_once_with(
            "all-MiniLM-L6-v2",
            cache_dir,
        )


# ---- Single Embedding ----


class TestGenerateEmbedding:
    """Tests for the generate_embedding method (single text)."""

    def test_returns_float_list(self) -> None:
        """generate_embedding should return a list of floats."""
        linker = EmbeddingLinker(config=EmbeddingsConfig())
        result = linker.generate_embedding("hello world")
        assert isinstance(result, list)
        assert all(isinstance(v, float) for v in result)

    def test_correct_dimensions(self) -> None:
        """generate_embedding should return vector with correct dimensions."""
        linker = EmbeddingLinker(config=EmbeddingsConfig())
        result = linker.generate_embedding("hello world")
        assert len(result) == _DIMS

    def test_consistent_results(self) -> None:
        """Same text should produce the same embedding."""
        linker = EmbeddingLinker(config=EmbeddingsConfig())
        first = linker.generate_embedding("hello")
        second = linker.generate_embedding("hello")
        assert first == second


# ---- Batch Embedding ----


class TestGenerateEmbeddings:
    """Tests for the generate_embeddings method (batch fragments)."""

    def test_returns_dict_with_fragment_ids(self) -> None:
        """generate_embeddings should return dict keyed by fragment IDs."""
        linker = EmbeddingLinker(config=EmbeddingsConfig())
        frags = [_make_fragment("A"), _make_fragment("B")]
        result = linker.generate_embeddings(frags)
        assert isinstance(result, dict)
        assert set(result.keys()) == {f.id for f in frags}

    def test_empty_input_returns_empty_dict(self) -> None:
        """generate_embeddings with empty list should return empty dict."""
        linker = EmbeddingLinker(config=EmbeddingsConfig())
        result = linker.generate_embeddings([])
        assert result == {}

    def test_embeddings_have_correct_dimensions(self) -> None:
        """Each embedding vector should have the correct number of dims."""
        linker = EmbeddingLinker(config=EmbeddingsConfig())
        frags = [_make_fragment("Test")]
        result = linker.generate_embeddings(frags)
        for vec in result.values():
            assert len(vec) == _DIMS

    def test_uses_fragment_title_for_text(
        self, mock_sentence_transformer: MagicMock
    ) -> None:
        """generate_embeddings should encode fragment titles."""
        mock_model = mock_sentence_transformer.return_value
        linker = EmbeddingLinker(config=EmbeddingsConfig())
        frags = [_make_fragment("Alpha"), _make_fragment("Beta")]
        linker.generate_embeddings(frags)
        call_args = mock_model.encode.call_args
        texts = call_args[0][0]
        assert "Alpha" in texts[0]
        assert "Beta" in texts[1]

    def test_passes_batch_size_from_config(
        self, mock_sentence_transformer: MagicMock
    ) -> None:
        """generate_embeddings should pass batch_size to model.encode."""
        mock_model = mock_sentence_transformer.return_value
        config = EmbeddingsConfig(batch_size=16)
        linker = EmbeddingLinker(config=config)
        frags = [_make_fragment("A")]
        linker.generate_embeddings(frags)
        call_kwargs = mock_model.encode.call_args[1]
        assert call_kwargs["batch_size"] == 16

    def test_progress_bar_follows_log_level(
        self, mock_sentence_transformer: MagicMock, caplog: pytest.LogCaptureFixture
    ) -> None:
        """show_progress_bar should reflect whether INFO logging is enabled."""
        mock_model = mock_sentence_transformer.return_value
        linker = EmbeddingLinker(config=EmbeddingsConfig())
        frags = [_make_fragment("A")]

        # With INFO enabled, show_progress_bar should be True
        with caplog.at_level(logging.INFO, logger="creek.link.embeddings"):
            linker._model = None  # reset cache to force reload
            linker.generate_embeddings(frags)
        call_kwargs = mock_model.encode.call_args[1]
        assert call_kwargs["show_progress_bar"] is True


# ---- Incremental Mode ----


class TestIncrementalMode:
    """Tests for incremental embedding generation."""

    def test_skips_existing_ids(self) -> None:
        """generate_embeddings should skip fragments in existing_ids."""
        linker = EmbeddingLinker(config=EmbeddingsConfig())
        frag_a = _make_fragment("A")
        frag_b = _make_fragment("B")
        result = linker.generate_embeddings(
            [frag_a, frag_b],
            existing_ids={frag_a.id},
        )
        assert frag_a.id not in result
        assert frag_b.id in result

    def test_all_existing_returns_empty(self) -> None:
        """If all fragments are in existing_ids, return empty dict."""
        linker = EmbeddingLinker(config=EmbeddingsConfig())
        frag = _make_fragment("A")
        result = linker.generate_embeddings([frag], existing_ids={frag.id})
        assert result == {}

    def test_no_existing_ids_processes_all(self) -> None:
        """Without existing_ids, all fragments should be processed."""
        linker = EmbeddingLinker(config=EmbeddingsConfig())
        frags = [_make_fragment("A"), _make_fragment("B")]
        result = linker.generate_embeddings(frags)
        assert len(result) == 2


# ---- Cache Save / Load ----


def _entry(
    fragment_id: str,
    *,
    vector: list[float],
    content_hash: str = "abc",
    model_name: str = "all-MiniLM-L6-v2",
    computed_at: datetime | None = None,
) -> CachedEmbedding:
    """Construct a CachedEmbedding with sensible defaults."""
    return CachedEmbedding(
        fragment_id=fragment_id,
        content_hash=content_hash,
        model_name=model_name,
        vector=vector,
        computed_at=computed_at or datetime(2026, 1, 1, tzinfo=UTC),
    )


class TestSaveLoadCache:
    """Tests for ``save_cache`` / ``load_cache`` parquet persistence (INC-006)."""

    def test_roundtrip(self, tmp_path: Path) -> None:
        """Cache entries should survive a parquet round-trip unchanged."""
        linker = EmbeddingLinker(config=EmbeddingsConfig())
        original = {
            "frag-001": _entry("frag-001", vector=[0.1, 0.2, 0.3]),
            "frag-002": _entry("frag-002", vector=[0.4, 0.5, 0.6]),
        }
        save_path = tmp_path / "embeddings.parquet"
        linker.save_cache(original, save_path)
        loaded = linker.load_cache(save_path)
        assert set(loaded.keys()) == set(original.keys())
        for key, entry in original.items():
            assert loaded[key].fragment_id == entry.fragment_id
            assert loaded[key].content_hash == entry.content_hash
            assert loaded[key].model_name == entry.model_name
            np.testing.assert_allclose(loaded[key].vector, entry.vector, rtol=1e-5)

    def test_save_creates_file(self, tmp_path: Path) -> None:
        """save_cache should create the parquet file."""
        linker = EmbeddingLinker(config=EmbeddingsConfig())
        save_path = tmp_path / "embeddings.parquet"
        linker.save_cache({"a": _entry("a", vector=[1.0])}, save_path)
        assert save_path.exists()

    def test_load_missing_file_returns_empty(self, tmp_path: Path) -> None:
        """load_cache should return an empty dict for a missing file.

        Treating a missing file as a cache miss lets callers fall
        through to a full recompute without special-casing FileNotFound.
        """
        linker = EmbeddingLinker(config=EmbeddingsConfig())
        assert linker.load_cache(tmp_path / "nonexistent.parquet") == {}

    def test_empty_cache_roundtrip(self, tmp_path: Path) -> None:
        """Saving and loading an empty cache should work."""
        linker = EmbeddingLinker(config=EmbeddingsConfig())
        save_path = tmp_path / "empty.parquet"
        linker.save_cache({}, save_path)
        loaded = linker.load_cache(save_path)
        assert loaded == {}

    def test_load_drops_entries_from_other_models(self, tmp_path: Path) -> None:
        """Cache entries for a non-active model must be invalidated on load."""
        save_path = tmp_path / "embeddings.parquet"
        writer = EmbeddingLinker(config=EmbeddingsConfig(model="old-model"))
        writer.save_cache(
            {
                "frag-001": _entry(
                    "frag-001",
                    vector=[0.1, 0.2],
                    model_name="old-model",
                ),
                "frag-002": _entry(
                    "frag-002",
                    vector=[0.3, 0.4],
                    model_name="new-model",
                ),
            },
            save_path,
        )
        reader = EmbeddingLinker(config=EmbeddingsConfig(model="new-model"))
        loaded = reader.load_cache(save_path)
        assert "frag-002" in loaded
        assert "frag-001" not in loaded


class TestContentHash:
    """Tests for the content_hash_for_text helper."""

    def test_stable_across_calls(self) -> None:
        """Hashing the same text twice yields identical digests."""
        assert content_hash_for_text("hello") == content_hash_for_text("hello")

    def test_different_text_different_hash(self) -> None:
        """Changing the source text changes the hash."""
        assert content_hash_for_text("hello") != content_hash_for_text("HELLO")

    def test_hex_digest_length(self) -> None:
        """SHA-256 hex digests are 64 characters."""
        assert len(content_hash_for_text("anything")) == 64


class TestBuildCacheEntries:
    """Tests for ``EmbeddingLinker.build_cache_entries``."""

    def test_entry_records_fragment_text_hash(self) -> None:
        """The content_hash should match the fragment's embedding text."""
        linker = EmbeddingLinker(config=EmbeddingsConfig(model="m1"))
        frag = _make_fragment("Alpha")
        entries = linker.build_cache_entries(
            [frag],
            {frag.id: [0.1, 0.2, 0.3]},
        )
        expected_hash = content_hash_for_text(fragment_embedding_text(frag))
        assert entries[frag.id].content_hash == expected_hash
        assert entries[frag.id].model_name == "m1"
        assert entries[frag.id].vector == [0.1, 0.2, 0.3]


class TestEmbeddingsCachePath:
    """Tests for the embeddings_cache_path helper."""

    def test_returns_canonical_parquet_path(self, tmp_path: Path) -> None:
        """The cache path lives in 00-Creek-Meta as a parquet file."""
        assert (
            embeddings_cache_path(tmp_path)
            == tmp_path / "00-Creek-Meta" / "embeddings.parquet"
        )


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

    def test_generate_embeddings_logs_count(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """generate_embeddings should log the number of fragments."""
        linker = EmbeddingLinker(config=EmbeddingsConfig())
        frags = [_make_fragment("A"), _make_fragment("B")]
        with caplog.at_level(logging.INFO, logger="creek.link.embeddings"):
            linker.generate_embeddings(frags)
        assert any("2" in r.message for r in caplog.records)

    def test_find_resonances_logs_count(self, caplog: pytest.LogCaptureFixture) -> None:
        """find_resonances should log the number of embeddings."""
        linker = EmbeddingLinker(config=EmbeddingsConfig())
        with caplog.at_level(logging.INFO, logger="creek.link.embeddings"):
            linker.find_resonances({"a": [1.0], "b": [0.0]})
        assert any("2" in r.message for r in caplog.records)
