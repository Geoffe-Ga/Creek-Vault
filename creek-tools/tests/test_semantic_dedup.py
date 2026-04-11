"""Tests for creek.clean.semantic_dedup — embedding-based near-duplicate detection.

Tests cover:
- SemanticDuplicateResult model (validation, serialization)
- SemanticDuplicatePair model (pair metadata, action classification)
- SemanticDeduplicator: batch mode (all-pairs comparison)
- SemanticDeduplicator: incremental mode (new fragment vs. existing index)
- SemanticDeduplicator: threshold tuning (duplicate vs. resonance vs. distinct)
- SemanticDeduplicator: threshold validation (invalid threshold combinations)
- SemanticDeduplicator: review queue generation
- SemanticDeduplicator: clear/reset
- SemanticDeduplicator: dimension validation
- Edge cases: empty embeddings, single fragment, zero vectors, dimension mismatch
- Cosine similarity correctness (using numpy)
"""

import math

import numpy as np
import pytest
from pydantic import ValidationError

from creek.clean.semantic_dedup import (
    SemanticDeduplicator,
    SemanticDuplicatePair,
    SemanticDuplicateResult,
    _cosine_similarity,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _to_np(vec: list[float]) -> np.ndarray:
    """Convert list to numpy array for _cosine_similarity."""
    return np.asarray(vec, dtype=np.float64)


# ---------------------------------------------------------------------------
# SemanticDuplicatePair model
# ---------------------------------------------------------------------------


class TestSemanticDuplicatePair:
    """Tests for the SemanticDuplicatePair Pydantic model."""

    def test_duplicate_pair_construction(self) -> None:
        """Should construct a valid pair with all fields."""
        pair = SemanticDuplicatePair(
            fragment_id_a="frag-aaa11111",
            fragment_id_b="frag-bbb22222",
            similarity=0.97,
            classification="duplicate",
        )
        assert pair.fragment_id_a == "frag-aaa11111"
        assert pair.fragment_id_b == "frag-bbb22222"
        assert pair.similarity == pytest.approx(0.97)
        assert pair.classification == "duplicate"

    def test_resonance_pair(self) -> None:
        """Should classify resonance pairs correctly."""
        pair = SemanticDuplicatePair(
            fragment_id_a="frag-aaa11111",
            fragment_id_b="frag-bbb22222",
            similarity=0.85,
            classification="resonance",
        )
        assert pair.classification == "resonance"

    def test_classification_literal_validation(self) -> None:
        """classification must be 'duplicate' or 'resonance'."""
        with pytest.raises(ValidationError):
            SemanticDuplicatePair(
                fragment_id_a="frag-aaa11111",
                fragment_id_b="frag-bbb22222",
                similarity=0.5,
                classification="invalid",
            )

    def test_similarity_range_allows_negative(self) -> None:
        """Cosine similarity can be negative; model should accept [-1, 1]."""
        pair = SemanticDuplicatePair(
            fragment_id_a="frag-a",
            fragment_id_b="frag-b",
            similarity=-0.5,
            classification="resonance",
        )
        assert pair.similarity == pytest.approx(-0.5)

    def test_similarity_rejects_below_minus_one(self) -> None:
        """Similarity below -1.0 should be rejected."""
        with pytest.raises(ValidationError):
            SemanticDuplicatePair(
                fragment_id_a="frag-a",
                fragment_id_b="frag-b",
                similarity=-1.5,
                classification="duplicate",
            )

    def test_similarity_rejects_above_one(self) -> None:
        """Similarity above 1.0 should be rejected."""
        with pytest.raises(ValidationError):
            SemanticDuplicatePair(
                fragment_id_a="frag-a",
                fragment_id_b="frag-b",
                similarity=1.5,
                classification="duplicate",
            )

    def test_serializable(self) -> None:
        """SemanticDuplicatePair should be JSON-serializable."""
        data = SemanticDuplicatePair(
            fragment_id_a="frag-aaa",
            fragment_id_b="frag-bbb",
            similarity=0.96,
            classification="duplicate",
        ).model_dump(mode="json")
        assert data["fragment_id_a"] == "frag-aaa"
        assert data["similarity"] == pytest.approx(0.96)


# ---------------------------------------------------------------------------
# SemanticDuplicateResult model
# ---------------------------------------------------------------------------


class TestSemanticDuplicateResult:
    """Tests for the SemanticDuplicateResult Pydantic model."""

    def test_empty_result(self) -> None:
        """Empty result should have zero counts and empty lists."""
        result = SemanticDuplicateResult(
            duplicates=[],
            resonances=[],
        )
        assert not result.duplicates
        assert not result.resonances

    def test_result_with_pairs(self) -> None:
        """Result should store both duplicate and resonance pairs."""
        dup = SemanticDuplicatePair(
            fragment_id_a="frag-a",
            fragment_id_b="frag-b",
            similarity=0.98,
            classification="duplicate",
        )
        res = SemanticDuplicatePair(
            fragment_id_a="frag-a",
            fragment_id_b="frag-c",
            similarity=0.82,
            classification="resonance",
        )
        result = SemanticDuplicateResult(duplicates=[dup], resonances=[res])
        assert len(result.duplicates) == 1
        assert len(result.resonances) == 1

    def test_serializable(self) -> None:
        """SemanticDuplicateResult should be JSON-serializable."""
        data = SemanticDuplicateResult(duplicates=[], resonances=[]).model_dump(
            mode="json"
        )
        assert data["duplicates"] == []
        assert data["resonances"] == []


# ---------------------------------------------------------------------------
# Cosine similarity function
# ---------------------------------------------------------------------------


class TestCosineSimilarity:
    """Tests for the _cosine_similarity helper function."""

    def test_identical_vectors(self) -> None:
        """Identical vectors should have similarity 1.0."""
        vec = _to_np([1.0, 2.0, 3.0])
        assert _cosine_similarity(vec, vec) == pytest.approx(1.0)

    def test_orthogonal_vectors(self) -> None:
        """Orthogonal vectors should have similarity 0.0."""
        a = _to_np([1.0, 0.0])
        b = _to_np([0.0, 1.0])
        assert _cosine_similarity(a, b) == pytest.approx(0.0)

    def test_opposite_vectors(self) -> None:
        """Opposite vectors should have similarity -1.0."""
        a = _to_np([1.0, 0.0])
        b = _to_np([-1.0, 0.0])
        assert _cosine_similarity(a, b) == pytest.approx(-1.0)

    def test_known_value(self) -> None:
        """Cosine similarity of known vectors."""
        a = _to_np([1.0, 2.0, 3.0])
        b = _to_np([4.0, 5.0, 6.0])
        dot = 1 * 4 + 2 * 5 + 3 * 6  # 32
        norm_a = math.sqrt(1 + 4 + 9)  # sqrt(14)
        norm_b = math.sqrt(16 + 25 + 36)  # sqrt(77)
        expected = dot / (norm_a * norm_b)
        assert _cosine_similarity(a, b) == pytest.approx(expected)

    def test_zero_vector_returns_zero(self) -> None:
        """Zero vector should return 0.0 similarity."""
        a = _to_np([0.0, 0.0, 0.0])
        b = _to_np([1.0, 2.0, 3.0])
        assert _cosine_similarity(a, b) == pytest.approx(0.0)

    def test_both_zero_vectors(self) -> None:
        """Both zero vectors should return 0.0."""
        a = _to_np([0.0, 0.0])
        b = _to_np([0.0, 0.0])
        assert _cosine_similarity(a, b) == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# SemanticDeduplicator — threshold validation
# ---------------------------------------------------------------------------


class TestThresholdValidation:
    """Tests for threshold validation on construction."""

    def test_resonance_equals_duplicate_raises(self) -> None:
        """Equal thresholds should raise ValueError."""
        with pytest.raises(ValueError, match="resonance_threshold"):
            SemanticDeduplicator(
                duplicate_threshold=0.80,
                resonance_threshold=0.80,
            )

    def test_resonance_above_duplicate_raises(self) -> None:
        """Resonance above duplicate should raise ValueError."""
        with pytest.raises(ValueError, match="resonance_threshold"):
            SemanticDeduplicator(
                duplicate_threshold=0.75,
                resonance_threshold=0.95,
            )

    def test_zero_resonance_raises(self) -> None:
        """Zero resonance threshold should raise ValueError."""
        with pytest.raises(ValueError, match="resonance_threshold"):
            SemanticDeduplicator(
                duplicate_threshold=0.95,
                resonance_threshold=0.0,
            )

    def test_duplicate_above_one_raises(self) -> None:
        """Duplicate threshold above 1.0 should raise ValueError."""
        with pytest.raises(ValueError, match="resonance_threshold"):
            SemanticDeduplicator(
                duplicate_threshold=1.5,
                resonance_threshold=0.75,
            )

    def test_negative_resonance_raises(self) -> None:
        """Negative resonance threshold should raise ValueError."""
        with pytest.raises(ValueError, match="resonance_threshold"):
            SemanticDeduplicator(
                duplicate_threshold=0.95,
                resonance_threshold=-0.1,
            )


# ---------------------------------------------------------------------------
# SemanticDeduplicator — dimension validation
# ---------------------------------------------------------------------------


class TestDimensionValidation:
    """Tests for embedding dimension validation."""

    def test_mismatched_add_fragment_raises(self) -> None:
        """Adding fragments with different dimensions should raise."""
        dedup = SemanticDeduplicator()
        dedup.add_fragment("frag-a", [1.0, 0.0, 0.0])
        with pytest.raises(ValueError, match=r"dimension mismatch.*frag-b"):
            dedup.add_fragment("frag-b", [1.0, 0.0])

    def test_mismatched_add_batch_raises(self) -> None:
        """Batch add with inconsistent dimensions should raise."""
        dedup = SemanticDeduplicator()
        dedup.add_fragment("frag-a", [1.0, 0.0, 0.0])
        with pytest.raises(ValueError, match="dimension mismatch"):
            dedup.add_batch({"frag-b": [1.0, 0.0]})

    def test_mismatched_find_duplicates_raises(self) -> None:
        """Batch find with inconsistent dimensions should raise."""
        dedup = SemanticDeduplicator()
        with pytest.raises(ValueError, match="dimension mismatch"):
            dedup.find_duplicates(
                {
                    "frag-a": [1.0, 0.0, 0.0],
                    "frag-b": [1.0, 0.0],
                }
            )

    def test_mismatched_check_fragment_raises(self) -> None:
        """Checking a fragment with wrong dimension should raise ValueError."""
        dedup = SemanticDeduplicator()
        dedup.add_fragment("frag-a", [1.0, 0.0, 0.0])
        with pytest.raises(ValueError, match=r"dimension mismatch.*frag-b"):
            dedup.check_fragment("frag-b", [1.0, 0.0])

    def test_clear_resets_dimension(self) -> None:
        """After clear, a different dimension should be accepted."""
        dedup = SemanticDeduplicator()
        dedup.add_fragment("frag-a", [1.0, 0.0, 0.0])
        dedup.clear()
        # Should not raise — dimension was reset
        dedup.add_fragment("frag-b", [1.0, 0.0])
        assert dedup.size == 1


# ---------------------------------------------------------------------------
# SemanticDeduplicator — batch mode
# ---------------------------------------------------------------------------


class TestBatchMode:
    """Tests for SemanticDeduplicator.find_duplicates (batch mode)."""

    def test_empty_embeddings(self) -> None:
        """Empty embeddings should produce empty result."""
        result = SemanticDeduplicator().find_duplicates({})
        assert not result.duplicates
        assert not result.resonances

    def test_single_fragment(self) -> None:
        """Single fragment should produce empty result (no pairs)."""
        dedup = SemanticDeduplicator()
        embeddings = {"frag-a": [1.0, 0.0, 0.0]}
        result = dedup.find_duplicates(embeddings)
        assert not result.duplicates
        assert not result.resonances

    def test_identical_embeddings_are_duplicate(self) -> None:
        """Identical embedding vectors should be classified as duplicates."""
        dedup = SemanticDeduplicator(duplicate_threshold=0.95)
        embeddings = {
            "frag-a": [1.0, 2.0, 3.0],
            "frag-b": [1.0, 2.0, 3.0],
        }
        result = dedup.find_duplicates(embeddings)
        assert len(result.duplicates) == 1
        assert result.duplicates[0].similarity == pytest.approx(1.0)
        assert result.duplicates[0].classification == "duplicate"

    def test_high_similarity_is_duplicate(self) -> None:
        """Vectors with similarity above duplicate_threshold are duplicates."""
        dedup = SemanticDeduplicator(duplicate_threshold=0.95)
        # Create two vectors that are very similar but not identical
        a = [1.0, 0.0, 0.0]
        b = [0.99, 0.1, 0.0]  # very similar to a
        embeddings = {"frag-a": a, "frag-b": b}
        result = dedup.find_duplicates(embeddings)
        assert len(result.duplicates) == 1
        assert result.duplicates[0].similarity >= 0.95

    def test_moderate_similarity_is_resonance(self) -> None:
        """Vectors between resonance and duplicate thresholds are resonances."""
        dedup = SemanticDeduplicator(
            duplicate_threshold=0.95,
            resonance_threshold=0.75,
        )
        # Create vectors with moderate similarity
        a = [1.0, 0.0, 0.0]
        b = [0.8, 0.6, 0.0]  # cosine sim ~0.8
        embeddings = {"frag-a": a, "frag-b": b}
        result = dedup.find_duplicates(embeddings)
        assert not result.duplicates
        assert len(result.resonances) == 1
        assert result.resonances[0].classification == "resonance"

    def test_low_similarity_not_flagged(self) -> None:
        """Vectors below resonance_threshold are not flagged."""
        dedup = SemanticDeduplicator(resonance_threshold=0.75)
        a = [1.0, 0.0, 0.0]
        b = [0.0, 1.0, 0.0]  # orthogonal, sim=0.0
        embeddings = {"frag-a": a, "frag-b": b}
        result = dedup.find_duplicates(embeddings)
        assert not result.duplicates
        assert not result.resonances

    def test_multiple_pairs(self) -> None:
        """Should detect multiple duplicate and resonance pairs."""
        dedup = SemanticDeduplicator(
            duplicate_threshold=0.95,
            resonance_threshold=0.75,
        )
        embeddings = {
            "frag-a": [1.0, 0.0, 0.0],
            "frag-b": [1.0, 0.0, 0.0],  # duplicate of a
            "frag-c": [0.8, 0.6, 0.0],  # resonance with a and b
            "frag-d": [0.0, 0.0, 1.0],  # unrelated
        }
        result = dedup.find_duplicates(embeddings)
        assert len(result.duplicates) == 1  # a-b
        # c has ~0.8 similarity with a and b
        assert result.resonances

    def test_pairs_sorted_by_similarity_descending(self) -> None:
        """Duplicate pairs should be sorted by similarity (highest first)."""
        dedup = SemanticDeduplicator(
            duplicate_threshold=0.90,
            resonance_threshold=0.75,
        )
        # Three fragments that are all duplicates of each other
        embeddings = {
            "frag-a": [1.0, 0.0, 0.0],
            "frag-b": [1.0, 0.0, 0.0],  # perfect duplicate
            "frag-c": [0.98, 0.2, 0.0],  # very similar
        }
        result = dedup.find_duplicates(embeddings)
        if len(result.duplicates) > 1:
            for i in range(len(result.duplicates) - 1):
                current = result.duplicates[i].similarity
                next_sim = result.duplicates[i + 1].similarity
                assert current >= next_sim


# ---------------------------------------------------------------------------
# SemanticDeduplicator — incremental mode
# ---------------------------------------------------------------------------


class TestIncrementalMode:
    """Tests for SemanticDeduplicator.check_fragment (incremental mode)."""

    def test_check_against_empty_index(self) -> None:
        """Checking against empty index should return empty result."""
        result = SemanticDeduplicator().check_fragment("frag-new", [1.0, 0.0, 0.0])
        assert not result.duplicates
        assert not result.resonances

    def test_add_then_check_duplicate(self) -> None:
        """Adding a fragment then checking a duplicate should detect it."""
        dedup = SemanticDeduplicator(duplicate_threshold=0.95)
        dedup.add_fragment("frag-a", [1.0, 0.0, 0.0])
        result = dedup.check_fragment("frag-b", [1.0, 0.0, 0.0])
        assert len(result.duplicates) == 1
        assert result.duplicates[0].fragment_id_a == "frag-b"
        assert result.duplicates[0].fragment_id_b == "frag-a"

    def test_add_then_check_resonance(self) -> None:
        """Adding a fragment then checking a resonance should detect it."""
        dedup = SemanticDeduplicator(
            duplicate_threshold=0.95,
            resonance_threshold=0.75,
        )
        dedup.add_fragment("frag-a", [1.0, 0.0, 0.0])
        result = dedup.check_fragment("frag-b", [0.8, 0.6, 0.0])
        assert len(result.resonances) == 1
        assert result.resonances[0].classification == "resonance"

    def test_add_then_check_unrelated(self) -> None:
        """Unrelated fragment should not be flagged."""
        dedup = SemanticDeduplicator(resonance_threshold=0.75)
        dedup.add_fragment("frag-a", [1.0, 0.0, 0.0])
        result = dedup.check_fragment("frag-b", [0.0, 1.0, 0.0])
        assert not result.duplicates
        assert not result.resonances

    def test_check_does_not_add_to_index(self) -> None:
        """check_fragment should not modify the index."""
        dedup = SemanticDeduplicator()
        dedup.check_fragment("frag-a", [1.0, 0.0, 0.0])
        assert dedup.size == 0

    def test_add_increments_size(self) -> None:
        """add_fragment should increase the index size."""
        dedup = SemanticDeduplicator()
        assert dedup.size == 0
        dedup.add_fragment("frag-a", [1.0, 0.0, 0.0])
        assert dedup.size == 1
        dedup.add_fragment("frag-b", [0.0, 1.0, 0.0])
        assert dedup.size == 2

    def test_add_multiple_then_check(self) -> None:
        """Checking against multiple indexed fragments works correctly."""
        dedup = SemanticDeduplicator(
            duplicate_threshold=0.95,
            resonance_threshold=0.75,
        )
        dedup.add_fragment("frag-a", [1.0, 0.0, 0.0])
        dedup.add_fragment("frag-b", [0.0, 1.0, 0.0])
        dedup.add_fragment("frag-c", [0.0, 0.0, 1.0])

        # Check a vector identical to frag-a
        result = dedup.check_fragment("frag-new", [1.0, 0.0, 0.0])
        assert len(result.duplicates) == 1
        assert result.duplicates[0].fragment_id_b == "frag-a"


# ---------------------------------------------------------------------------
# SemanticDeduplicator — threshold tuning
# ---------------------------------------------------------------------------


class TestThresholdTuning:
    """Tests for configurable threshold behaviour."""

    def test_default_thresholds(self) -> None:
        """Default thresholds should be duplicate=0.95, resonance=0.75."""
        dedup = SemanticDeduplicator()
        assert dedup.duplicate_threshold == pytest.approx(0.95)
        assert dedup.resonance_threshold == pytest.approx(0.75)

    def test_custom_thresholds(self) -> None:
        """Custom thresholds should be stored correctly."""
        dedup = SemanticDeduplicator(
            duplicate_threshold=0.90,
            resonance_threshold=0.60,
        )
        assert dedup.duplicate_threshold == pytest.approx(0.90)
        assert dedup.resonance_threshold == pytest.approx(0.60)

    def test_higher_duplicate_threshold_fewer_duplicates(self) -> None:
        """Higher duplicate threshold should produce fewer duplicates."""
        a = [1.0, 0.0, 0.0]
        b = [0.98, 0.2, 0.0]  # similarity ~0.98
        embeddings = {"frag-a": a, "frag-b": b}

        # Lower threshold — should detect as duplicate
        result_loose = SemanticDeduplicator(duplicate_threshold=0.90).find_duplicates(
            embeddings
        )

        # Higher threshold — may not detect as duplicate
        result_strict = SemanticDeduplicator(duplicate_threshold=0.999).find_duplicates(
            embeddings
        )

        assert len(result_loose.duplicates) >= len(result_strict.duplicates)

    def test_resonance_threshold_boundary(self) -> None:
        """Similarity exactly at resonance_threshold should be included."""
        dedup = SemanticDeduplicator(
            duplicate_threshold=0.95,
            resonance_threshold=0.75,
        )
        # Build vectors with known cosine similarity of exactly 0.75
        # cos(theta) = 0.75, so a=[1,0], b=[0.75, sqrt(1-0.75^2)]
        sin_val = math.sqrt(1 - 0.75**2)
        a = [1.0, 0.0]
        b = [0.75, sin_val]
        embeddings = {"frag-a": a, "frag-b": b}
        result = dedup.find_duplicates(embeddings)
        # Should be classified as resonance (>= threshold)
        assert len(result.resonances) == 1


# ---------------------------------------------------------------------------
# SemanticDeduplicator — clear/reset
# ---------------------------------------------------------------------------


class TestClear:
    """Tests for SemanticDeduplicator.clear."""

    def test_clear_empties_index(self) -> None:
        """clear() should remove all indexed fragments."""
        dedup = SemanticDeduplicator()
        dedup.add_fragment("frag-a", [1.0, 0.0])
        dedup.add_fragment("frag-b", [0.0, 1.0])
        assert dedup.size == 2
        dedup.clear()
        assert dedup.size == 0

    def test_clear_resets_detection(self) -> None:
        """After clear(), previously-indexed fragments should not be found."""
        dedup = SemanticDeduplicator(duplicate_threshold=0.95)
        dedup.add_fragment("frag-a", [1.0, 0.0, 0.0])
        dedup.clear()
        result = dedup.check_fragment("frag-b", [1.0, 0.0, 0.0])
        assert not result.duplicates


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    """Tests for edge cases in semantic deduplication."""

    def test_high_dimensional_vectors(self) -> None:
        """Should handle high-dimensional embedding vectors."""
        dedup = SemanticDeduplicator(duplicate_threshold=0.95)
        dim = 384  # typical sentence-transformer dimension
        vec_a = [1.0 / math.sqrt(dim)] * dim
        vec_b = [1.0 / math.sqrt(dim)] * dim
        embeddings = {"frag-a": vec_a, "frag-b": vec_b}
        result = dedup.find_duplicates(embeddings)
        assert len(result.duplicates) == 1

    def test_negative_values_in_embeddings(self) -> None:
        """Should handle negative values in embedding vectors."""
        dedup = SemanticDeduplicator(duplicate_threshold=0.95)
        a = [-1.0, -2.0, -3.0]
        b = [-1.0, -2.0, -3.0]
        embeddings = {"frag-a": a, "frag-b": b}
        result = dedup.find_duplicates(embeddings)
        assert len(result.duplicates) == 1

    def test_add_replaces_existing_embedding(self) -> None:
        """Adding a fragment with same ID should update the embedding."""
        dedup = SemanticDeduplicator()
        dedup.add_fragment("frag-a", [1.0, 0.0])
        dedup.add_fragment("frag-a", [0.0, 1.0])
        assert dedup.size == 1  # still one fragment

    def test_batch_with_many_fragments(self) -> None:
        """Batch mode should handle many fragments without error."""
        dedup = SemanticDeduplicator(
            duplicate_threshold=0.95,
            resonance_threshold=0.75,
        )
        # 50 orthogonal-ish vectors (no duplicates expected)
        embeddings: dict[str, list[float]] = {}
        for i in range(50):
            vec = [0.0] * 50
            vec[i] = 1.0
            embeddings[f"frag-{i:03d}"] = vec
        result = dedup.find_duplicates(embeddings)
        assert not result.duplicates
        assert not result.resonances


# ---------------------------------------------------------------------------
# Integration: batch populates index for incremental use
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestBatchAndIncrementalIntegration:
    """Integration: batch results can feed incremental index."""

    def test_add_batch_then_check_incremental(self) -> None:
        """After adding batch embeddings, incremental check should work."""
        dedup = SemanticDeduplicator(duplicate_threshold=0.95)
        # Add a batch of embeddings to the index
        batch = {
            "frag-a": [1.0, 0.0, 0.0],
            "frag-b": [0.0, 1.0, 0.0],
            "frag-c": [0.0, 0.0, 1.0],
        }
        dedup.add_batch(batch)
        assert dedup.size == 3

        # Check a new fragment that duplicates frag-a
        result = dedup.check_fragment("frag-new", [1.0, 0.0, 0.0])
        assert len(result.duplicates) == 1
        assert result.duplicates[0].fragment_id_b == "frag-a"
