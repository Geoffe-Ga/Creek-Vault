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
    Resonance,
    content_hash_for_text,
    embeddings_cache_path,
    fragment_embedding_text,
)
from creek.models import Fragment, FragmentLevel, FragmentSource, SourcePlatform

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


class TestPurgeFragmentIdsFromCache:
    """Tests for ``purge_fragment_ids_from_cache`` (GAP-001)."""

    def _seed_cache(self, tmp_path: Path, ids: list[str]) -> Path:
        """Write a parquet cache with one row per supplied fragment id."""
        linker = EmbeddingLinker(config=EmbeddingsConfig())
        cache_path = tmp_path / "embeddings.parquet"
        linker.save_cache(
            {fid: _entry(fid, vector=[0.1, 0.2]) for fid in ids},
            cache_path,
        )
        return cache_path

    def test_returns_zero_when_cache_missing(self, tmp_path: Path) -> None:
        """A missing cache file is treated as zero rows removed, not an error."""
        from creek.link.embeddings import purge_fragment_ids_from_cache

        removed = purge_fragment_ids_from_cache(
            tmp_path / "nonexistent.parquet",
            ["frag-A"],
        )
        assert removed == 0

    def test_returns_zero_for_empty_id_set(self, tmp_path: Path) -> None:
        """An empty id collection is a no-op even when the cache exists."""
        from creek.link.embeddings import purge_fragment_ids_from_cache

        cache_path = self._seed_cache(tmp_path, ["frag-A", "frag-B"])
        removed = purge_fragment_ids_from_cache(cache_path, [])
        assert removed == 0
        # Cache untouched.
        linker = EmbeddingLinker(config=EmbeddingsConfig())
        assert set(linker.load_cache(cache_path).keys()) == {"frag-A", "frag-B"}

    def test_removes_matching_rows_only(self, tmp_path: Path) -> None:
        """Only rows whose fragment_id appears in the purge set are removed."""
        from creek.link.embeddings import purge_fragment_ids_from_cache

        cache_path = self._seed_cache(
            tmp_path,
            ["frag-A", "frag-B", "frag-C"],
        )

        removed = purge_fragment_ids_from_cache(cache_path, ["frag-A", "frag-C"])

        assert removed == 2
        linker = EmbeddingLinker(config=EmbeddingsConfig())
        survivors = linker.load_cache(cache_path)
        assert set(survivors.keys()) == {"frag-B"}

    def test_returns_zero_when_no_ids_match(self, tmp_path: Path) -> None:
        """Purging IDs that aren't in the cache reports zero removals."""
        from creek.link.embeddings import purge_fragment_ids_from_cache

        cache_path = self._seed_cache(tmp_path, ["frag-A"])

        removed = purge_fragment_ids_from_cache(cache_path, ["frag-missing"])

        assert removed == 0
        linker = EmbeddingLinker(config=EmbeddingsConfig())
        assert set(linker.load_cache(cache_path).keys()) == {"frag-A"}

    def test_purging_every_row_keeps_file(self, tmp_path: Path) -> None:
        """Removing every row leaves an empty parquet on disk, not a missing file."""
        from creek.link.embeddings import purge_fragment_ids_from_cache

        cache_path = self._seed_cache(tmp_path, ["frag-A", "frag-B"])

        removed = purge_fragment_ids_from_cache(cache_path, ["frag-A", "frag-B"])

        assert removed == 2
        assert cache_path.exists()
        linker = EmbeddingLinker(config=EmbeddingsConfig())
        assert linker.load_cache(cache_path) == {}

    def test_dry_run_counts_without_writing(self, tmp_path: Path) -> None:
        """``dry_run=True`` reports the would-be count without rewriting the file."""
        from creek.link.embeddings import purge_fragment_ids_from_cache

        cache_path = self._seed_cache(tmp_path, ["frag-A", "frag-B"])
        before_bytes = cache_path.read_bytes()

        removed = purge_fragment_ids_from_cache(
            cache_path,
            ["frag-A"],
            dry_run=True,
        )

        assert removed == 1
        assert cache_path.read_bytes() == before_bytes


class TestDeleteEmbeddingsCache:
    """Tests for ``delete_embeddings_cache`` (GAP-001 vault path)."""

    def test_unlinks_existing_cache_and_returns_row_count(
        self,
        tmp_path: Path,
    ) -> None:
        """The whole file is removed; the return value is the prior row count."""
        from creek.link.embeddings import delete_embeddings_cache

        linker = EmbeddingLinker(config=EmbeddingsConfig())
        cache_path = tmp_path / "embeddings.parquet"
        linker.save_cache(
            {
                "frag-A": _entry("frag-A", vector=[0.1]),
                "frag-B": _entry("frag-B", vector=[0.2]),
            },
            cache_path,
        )

        removed = delete_embeddings_cache(cache_path)

        assert removed == 2
        assert not cache_path.exists()

    def test_returns_zero_when_cache_missing(self, tmp_path: Path) -> None:
        """A missing cache is a no-op, not an error."""
        from creek.link.embeddings import delete_embeddings_cache

        assert delete_embeddings_cache(tmp_path / "nope.parquet") == 0

    def test_dry_run_preserves_cache_file(self, tmp_path: Path) -> None:
        """``dry_run=True`` still reports the row count but keeps the file."""
        from creek.link.embeddings import delete_embeddings_cache

        linker = EmbeddingLinker(config=EmbeddingsConfig())
        cache_path = tmp_path / "embeddings.parquet"
        linker.save_cache(
            {"frag-A": _entry("frag-A", vector=[0.1])},
            cache_path,
        )

        removed = delete_embeddings_cache(cache_path, dry_run=True)

        assert removed == 1
        assert cache_path.exists()


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
        assert result[0].fragment_a_id == "a"
        assert result[0].fragment_b_id == "b"
        assert abs(result[0].similarity - 1.0) < 1e-6

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
        pair_ids = {(r.fragment_a_id, r.fragment_b_id) for r in result}
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

    def test_result_record_structure(self) -> None:
        """Each resonance should be a Resonance with id/sim/level fields."""
        linker = EmbeddingLinker(
            config=EmbeddingsConfig(similarity_threshold=0.0),
        )
        embeddings = {
            "a": [1.0, 0.0],
            "b": [0.7, 0.7],
        }
        result = linker.find_resonances(embeddings)
        assert len(result) >= 1
        edge = result[0]
        assert isinstance(edge.fragment_a_id, str)
        assert isinstance(edge.fragment_b_id, str)
        assert isinstance(edge.similarity, float)
        assert 0.0 <= edge.similarity <= 1.0
        # Without a fragments map, both endpoints default to "document".
        assert edge.from_level == "document"
        assert edge.to_level == "document"

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
        pairs = [(r.fragment_a_id, r.fragment_b_id) for r in result]
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


# ---- FEAT-024 Hierarchy-aware Filtering ----


def _hier_fragment(
    fid: str,
    *,
    parent_id: str | None = None,
    child_ids: list[str] | None = None,
    level: FragmentLevel = "document",
) -> Fragment:
    """Build a Fragment with explicit hierarchy fields for FEAT-024 tests."""
    return Fragment(
        id=fid,
        title=fid,
        source=FragmentSource(platform=SourcePlatform.CLAUDE),
        parent_id=parent_id,
        child_ids=child_ids or [],
        level=level,
    )


class TestHierarchyAwareFiltering:
    """Tests for FEAT-024 hierarchy-aware resonance suppression."""

    def test_ancestor_descendant_pair_suppressed(self) -> None:
        """Parent/child pairs never produce a resonance, even at sim=1.0."""
        linker = EmbeddingLinker(
            config=EmbeddingsConfig(similarity_threshold=0.5),
        )
        fragments = {
            "p": _hier_fragment(
                "p",
                child_ids=["c1"],
                level="document",
            ),
            "c1": _hier_fragment("c1", parent_id="p", level="paragraph"),
        }
        embeddings = {"p": [1.0, 0.0], "c1": [1.0, 0.0]}
        result = linker.find_resonances(embeddings, fragments)
        assert not result

    def test_deep_ancestor_descendant_suppressed(self) -> None:
        """Suppression follows the parent_id chain past a single hop."""
        linker = EmbeddingLinker(
            config=EmbeddingsConfig(similarity_threshold=0.5),
        )
        fragments = {
            "root": _hier_fragment(
                "root",
                child_ids=["mid"],
                level="document",
            ),
            "mid": _hier_fragment(
                "mid",
                parent_id="root",
                child_ids=["leaf"],
                level="paragraph",
            ),
            "leaf": _hier_fragment("leaf", parent_id="mid", level="sentence"),
        }
        embeddings = {"root": [1.0, 0.0], "mid": [1.0, 0.0], "leaf": [1.0, 0.0]}
        result = linker.find_resonances(embeddings, fragments)
        # No surviving pair — root-mid, mid-leaf, root-leaf are all ancestor edges.
        assert not result

    def test_adjacent_siblings_within_default_window_suppressed(self) -> None:
        """Siblings within K=2 positions of each other are suppressed."""
        linker = EmbeddingLinker(
            config=EmbeddingsConfig(similarity_threshold=0.5),
        )
        fragments = {
            "p": _hier_fragment(
                "p",
                child_ids=["s0", "s1", "s2"],
                level="document",
            ),
            "s0": _hier_fragment("s0", parent_id="p", level="sentence"),
            "s1": _hier_fragment("s1", parent_id="p", level="sentence"),
            "s2": _hier_fragment("s2", parent_id="p", level="sentence"),
        }
        embeddings = {
            "s0": [1.0, 0.0],
            "s1": [1.0, 0.0],
            "s2": [1.0, 0.0],
        }
        result = linker.find_resonances(embeddings, fragments)
        # All three sentence-siblings sit within window=2 of each other.
        pair_ids = {(r.fragment_a_id, r.fragment_b_id) for r in result}
        assert ("s0", "s1") not in pair_ids
        assert ("s1", "s2") not in pair_ids
        assert ("s0", "s2") not in pair_ids

    def test_non_adjacent_siblings_kept(self) -> None:
        """Siblings beyond the skip window keep their resonance edge."""
        linker = EmbeddingLinker(
            config=EmbeddingsConfig(similarity_threshold=0.5),
        )
        fragments = {
            "p": _hier_fragment(
                "p",
                child_ids=["c0", "c1", "c2", "c3"],
                level="document",
            ),
            "c0": _hier_fragment("c0", parent_id="p", level="sentence"),
            "c1": _hier_fragment("c1", parent_id="p", level="sentence"),
            "c2": _hier_fragment("c2", parent_id="p", level="sentence"),
            "c3": _hier_fragment("c3", parent_id="p", level="sentence"),
        }
        # Make only the (c0, c3) pair similar enough to survive.
        embeddings = {
            "c0": [1.0, 0.0],
            "c1": [0.0, 1.0],
            "c2": [0.0, 1.0],
            "c3": [1.0, 0.0],
        }
        result = linker.find_resonances(embeddings, fragments)
        pair_ids = {(r.fragment_a_id, r.fragment_b_id) for r in result}
        # c0 and c3 are 3 positions apart — outside the default window of 2.
        assert ("c0", "c3") in pair_ids

    def test_cross_tree_pair_kept(self) -> None:
        """Fragments under different parents are never suppressed."""
        linker = EmbeddingLinker(
            config=EmbeddingsConfig(similarity_threshold=0.5),
        )
        fragments = {
            "p1": _hier_fragment("p1", child_ids=["a"], level="document"),
            "p2": _hier_fragment("p2", child_ids=["b"], level="document"),
            "a": _hier_fragment("a", parent_id="p1", level="paragraph"),
            "b": _hier_fragment("b", parent_id="p2", level="paragraph"),
        }
        embeddings = {"a": [1.0, 0.0], "b": [1.0, 0.0]}
        result = linker.find_resonances(embeddings, fragments)
        pair_ids = {(r.fragment_a_id, r.fragment_b_id) for r in result}
        assert ("a", "b") in pair_ids

    def test_resonance_records_endpoint_levels(self) -> None:
        """Returned Resonance carries from_level / to_level from fragments."""
        linker = EmbeddingLinker(
            config=EmbeddingsConfig(similarity_threshold=0.5),
        )
        fragments = {
            "p1": _hier_fragment("p1", child_ids=["a"], level="exchange"),
            "p2": _hier_fragment("p2", child_ids=["b"], level="document"),
            "a": _hier_fragment("a", parent_id="p1", level="sentence"),
            "b": _hier_fragment("b", parent_id="p2", level="paragraph"),
        }
        embeddings = {"a": [1.0, 0.0], "b": [1.0, 0.0]}
        result = linker.find_resonances(embeddings, fragments)
        assert len(result) == 1
        edge = result[0]
        assert edge.from_level == "sentence"
        assert edge.to_level == "paragraph"

    def test_flat_fragment_regression(self) -> None:
        """No-hierarchy vaults produce the same edges as pre-FEAT-024.

        Acceptance criterion: existing flat-fragment behaviour is
        unchanged when no parent/child fields are populated.
        """
        baseline = EmbeddingLinker(
            config=EmbeddingsConfig(similarity_threshold=0.5),
        )
        embeddings = {
            "a": [1.0, 0.0],
            "b": [0.95, 0.05],
            "c": [0.0, 1.0],
        }
        without_fragments = baseline.find_resonances(embeddings)
        flat_fragments = {
            "a": _hier_fragment("a"),
            "b": _hier_fragment("b"),
            "c": _hier_fragment("c"),
        }
        with_fragments = baseline.find_resonances(embeddings, flat_fragments)
        # Same pair set, in the same order.
        pairs_a = [(r.fragment_a_id, r.fragment_b_id) for r in without_fragments]
        pairs_b = [(r.fragment_a_id, r.fragment_b_id) for r in with_fragments]
        assert pairs_a == pairs_b
        # Levels default to "document" in both cases.
        for edge in with_fragments:
            assert edge.from_level == "document"
            assert edge.to_level == "document"

    def test_skip_window_zero_disables_sibling_suppression(self) -> None:
        """A skip window of 0 leaves siblings in the graph."""
        linker = EmbeddingLinker(
            config=EmbeddingsConfig(similarity_threshold=0.5),
        )
        fragments = {
            "p": _hier_fragment(
                "p",
                child_ids=["s0", "s1"],
                level="document",
            ),
            "s0": _hier_fragment("s0", parent_id="p", level="sentence"),
            "s1": _hier_fragment("s1", parent_id="p", level="sentence"),
        }
        embeddings = {"s0": [1.0, 0.0], "s1": [1.0, 0.0]}
        result = linker.find_resonances(
            embeddings,
            fragments,
            sibling_skip_window=0,
        )
        pair_ids = {(r.fragment_a_id, r.fragment_b_id) for r in result}
        assert ("s0", "s1") in pair_ids

    def test_skip_window_zero_still_suppresses_ancestors(self) -> None:
        """Skip window 0 does NOT relax the ancestor suppression."""
        linker = EmbeddingLinker(
            config=EmbeddingsConfig(similarity_threshold=0.5),
        )
        fragments = {
            "p": _hier_fragment("p", child_ids=["c"], level="document"),
            "c": _hier_fragment("c", parent_id="p", level="paragraph"),
        }
        embeddings = {"p": [1.0, 0.0], "c": [1.0, 0.0]}
        result = linker.find_resonances(
            embeddings,
            fragments,
            sibling_skip_window=0,
        )
        assert not result

    def test_cycle_in_parent_chain_is_tolerated(self) -> None:
        """A malformed cyclic parent_id chain must not loop forever."""
        linker = EmbeddingLinker(
            config=EmbeddingsConfig(similarity_threshold=0.5),
        )
        # Hand-crafted cycle: a -> b -> a. The linker should fall through
        # without hanging, even though the data is broken.
        fragments = {
            "a": _hier_fragment("a", parent_id="b", level="paragraph"),
            "b": _hier_fragment("b", parent_id="a", level="paragraph"),
        }
        embeddings = {"a": [1.0, 0.0], "b": [1.0, 0.0]}
        # Each side sees the other in its ancestor set, so the pair is
        # still suppressed — what matters is that we don't hang.
        result = linker.find_resonances(embeddings, fragments)
        assert not result

    def test_resonance_model_default_levels(self) -> None:
        """Constructing a Resonance with no level kwargs yields 'document'."""
        edge = Resonance(
            fragment_a_id="x",
            fragment_b_id="y",
            similarity=0.91,
        )
        assert edge.from_level == "document"
        assert edge.to_level == "document"
