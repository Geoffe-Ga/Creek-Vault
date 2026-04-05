"""Embedding-based near-duplicate detection — cross-source semantic dedup.

Detects near-duplicate fragments by comparing pre-computed embedding vectors
using cosine similarity.  Unlike the normalized deduplication in
:mod:`creek.clean.dedup`, this module catches semantic duplicates — content
that expresses the same idea in different words across different sources
(e.g. a Discord message and a journal entry).

Two classification tiers:

- **Duplicate**: Similarity at or above ``duplicate_threshold`` (default 0.95).
  These are near-identical in meaning and should be reviewed for merging.
- **Resonance**: Similarity between ``resonance_threshold`` (default 0.75) and
  ``duplicate_threshold``.  These are related but distinct ideas that may
  represent genuine semantic connections rather than duplicates.

All flagged pairs are placed in a review queue — nothing is auto-deleted.
Supports both **batch** (all-pairs comparison) and **incremental** (new
fragment vs. existing index) processing modes.
"""

import math
from typing import Literal

from pydantic import BaseModel, Field


def _cosine_similarity(vec_a: list[float], vec_b: list[float]) -> float:
    """Compute cosine similarity between two vectors.

    Returns 0.0 when either vector has zero magnitude, avoiding
    division-by-zero errors.

    Args:
        vec_a: First embedding vector.
        vec_b: Second embedding vector.

    Returns:
        Cosine similarity in the range [-1.0, 1.0], or 0.0 if either
        vector has zero magnitude.
    """
    dot = sum(a * b for a, b in zip(vec_a, vec_b, strict=True))
    norm_a = math.sqrt(sum(a * a for a in vec_a))
    norm_b = math.sqrt(sum(b * b for b in vec_b))

    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0

    return dot / (norm_a * norm_b)


# ---------------------------------------------------------------------------
# Result models
# ---------------------------------------------------------------------------


class SemanticDuplicatePair(BaseModel):
    """A pair of fragments flagged by semantic similarity.

    Attributes:
        fragment_id_a: ID of the first fragment in the pair.
        fragment_id_b: ID of the second fragment in the pair.
        similarity: Cosine similarity between the two embedding vectors.
        classification: Whether the pair is a ``duplicate`` (very high
            similarity) or a ``resonance`` (moderate similarity).
    """

    fragment_id_a: str
    fragment_id_b: str
    similarity: float = Field(ge=0.0, le=1.0)
    classification: Literal["duplicate", "resonance"]


class SemanticDuplicateResult(BaseModel):
    """Aggregated result of a semantic deduplication pass.

    Attributes:
        duplicates: Pairs classified as near-duplicates, sorted by
            similarity descending.
        resonances: Pairs classified as resonances (related but distinct),
            sorted by similarity descending.
    """

    duplicates: list[SemanticDuplicatePair]
    resonances: list[SemanticDuplicatePair]


# ---------------------------------------------------------------------------
# Deduplicator
# ---------------------------------------------------------------------------


class SemanticDeduplicator:
    """Embedding-based near-duplicate detector for Creek fragments.

    Maintains an in-memory index of fragment embeddings and provides
    both batch (all-pairs) and incremental (new-vs-existing) detection
    modes.  Flagged pairs are classified as either **duplicates** or
    **resonances** based on configurable cosine-similarity thresholds.

    Attributes:
        duplicate_threshold: Minimum cosine similarity for a pair to be
            classified as a near-duplicate.
        resonance_threshold: Minimum cosine similarity for a pair to be
            classified as a resonance (must be below
            ``duplicate_threshold``).
    """

    def __init__(
        self,
        *,
        duplicate_threshold: float = 0.95,
        resonance_threshold: float = 0.75,
    ) -> None:
        """Initialise the deduplicator with configurable thresholds.

        Args:
            duplicate_threshold: Cosine similarity at or above which a
                pair is classified as a near-duplicate.
            resonance_threshold: Cosine similarity at or above which
                (but below ``duplicate_threshold``) a pair is classified
                as a resonance.
        """
        self.duplicate_threshold = duplicate_threshold
        self.resonance_threshold = resonance_threshold
        self._index: dict[str, list[float]] = {}

    @property
    def size(self) -> int:
        """Return the number of indexed fragments.

        Returns:
            Count of fragments in the embedding index.
        """
        return len(self._index)

    def add_fragment(self, fragment_id: str, embedding: list[float]) -> None:
        """Add a fragment embedding to the index.

        If a fragment with the same ID already exists, its embedding is
        replaced.

        Args:
            fragment_id: Unique identifier for the fragment.
            embedding: Pre-computed embedding vector.
        """
        self._index[fragment_id] = embedding

    def add_batch(self, embeddings: dict[str, list[float]]) -> None:
        """Add multiple fragment embeddings to the index.

        Args:
            embeddings: Mapping of fragment IDs to embedding vectors.
        """
        self._index.update(embeddings)

    def clear(self) -> None:
        """Remove all fragments from the index."""
        self._index.clear()

    def find_duplicates(
        self,
        embeddings: dict[str, list[float]],
    ) -> SemanticDuplicateResult:
        """Find near-duplicates and resonances in a batch of embeddings.

        Compares all unique pairs and classifies each by cosine
        similarity.  Does **not** modify the internal index.

        Args:
            embeddings: Mapping of fragment IDs to embedding vectors.

        Returns:
            A :class:`SemanticDuplicateResult` with duplicate and
            resonance pairs sorted by similarity descending.
        """
        duplicates: list[SemanticDuplicatePair] = []
        resonances: list[SemanticDuplicatePair] = []

        ids = sorted(embeddings.keys())
        for i in range(len(ids)):
            for j in range(i + 1, len(ids)):
                sim = _cosine_similarity(embeddings[ids[i]], embeddings[ids[j]])
                pair = self._classify_pair(ids[i], ids[j], sim)
                if pair is not None:
                    if pair.classification == "duplicate":
                        duplicates.append(pair)
                    else:
                        resonances.append(pair)

        duplicates.sort(key=lambda p: p.similarity, reverse=True)
        resonances.sort(key=lambda p: p.similarity, reverse=True)

        return SemanticDuplicateResult(
            duplicates=duplicates,
            resonances=resonances,
        )

    def check_fragment(
        self,
        fragment_id: str,
        embedding: list[float],
    ) -> SemanticDuplicateResult:
        """Check a new fragment against the existing index (incremental mode).

        Compares the given embedding against all indexed fragments
        without modifying the index.  Use :meth:`add_fragment` to add
        the fragment to the index after reviewing results.

        Args:
            fragment_id: ID of the fragment to check.
            embedding: Pre-computed embedding vector for the fragment.

        Returns:
            A :class:`SemanticDuplicateResult` with matches found in
            the index.
        """
        duplicates: list[SemanticDuplicatePair] = []
        resonances: list[SemanticDuplicatePair] = []

        for indexed_id, indexed_vec in self._index.items():
            sim = _cosine_similarity(embedding, indexed_vec)
            pair = self._classify_pair(fragment_id, indexed_id, sim)
            if pair is not None:
                if pair.classification == "duplicate":
                    duplicates.append(pair)
                else:
                    resonances.append(pair)

        duplicates.sort(key=lambda p: p.similarity, reverse=True)
        resonances.sort(key=lambda p: p.similarity, reverse=True)

        return SemanticDuplicateResult(
            duplicates=duplicates,
            resonances=resonances,
        )

    def _classify_pair(
        self,
        id_a: str,
        id_b: str,
        similarity: float,
    ) -> SemanticDuplicatePair | None:
        """Classify a fragment pair based on cosine similarity.

        Returns ``None`` if the similarity is below the resonance
        threshold.

        Args:
            id_a: ID of the first fragment.
            id_b: ID of the second fragment.
            similarity: Cosine similarity between the two embeddings.

        Returns:
            A :class:`SemanticDuplicatePair` if above the resonance
            threshold, or ``None`` if below.
        """
        if similarity >= self.duplicate_threshold:
            return SemanticDuplicatePair(
                fragment_id_a=id_a,
                fragment_id_b=id_b,
                similarity=similarity,
                classification="duplicate",
            )
        if similarity >= self.resonance_threshold:
            return SemanticDuplicatePair(
                fragment_id_a=id_a,
                fragment_id_b=id_b,
                similarity=similarity,
                classification="resonance",
            )
        return None
