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

Embedding vectors must all share the same dimensionality.  Mismatched
dimensions will raise a ``ValueError`` with context about the offending
fragment and expected size.
"""

from typing import Literal

import numpy as np
from pydantic import BaseModel, Field


def _cosine_similarity(vec_a: np.ndarray, vec_b: np.ndarray) -> float:
    """Compute cosine similarity between two numpy vectors.

    Returns 0.0 when either vector has zero magnitude, avoiding
    division-by-zero errors.

    Args:
        vec_a: First embedding vector.
        vec_b: Second embedding vector.

    Returns:
        Cosine similarity in the range [-1.0, 1.0], or 0.0 if either
        vector has zero magnitude.
    """
    norm_a = np.linalg.norm(vec_a)
    norm_b = np.linalg.norm(vec_b)

    if 0.0 in (norm_a, norm_b):
        return 0.0

    return float(np.dot(vec_a, vec_b) / (norm_a * norm_b))


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
    similarity: float = Field(ge=-1.0, le=1.0)
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

    All embedding vectors must have the same dimensionality.  The expected
    dimension is set by the first vector added and enforced on subsequent
    additions.

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

        Raises:
            ValueError: If ``resonance_threshold`` is not less than
                ``duplicate_threshold``, or if either is outside (0, 1].
        """
        if not (0.0 < resonance_threshold < duplicate_threshold <= 1.0):
            msg = (
                f"resonance_threshold ({resonance_threshold}) must be "
                f"less than duplicate_threshold ({duplicate_threshold}) "
                f"and both must be in (0, 1]"
            )
            raise ValueError(msg)

        self.duplicate_threshold = duplicate_threshold
        self.resonance_threshold = resonance_threshold
        self._index: dict[str, np.ndarray] = {}
        self._dimension: int | None = None

    def _validate_embedding(
        self,
        fragment_id: str,
        embedding: list[float],
    ) -> np.ndarray:
        """Validate and convert an embedding to a numpy array.

        Checks dimensionality against the expected dimension (set by
        the first embedding added).

        Args:
            fragment_id: ID of the fragment (for error messages).
            embedding: Raw embedding vector as a list of floats.

        Returns:
            The embedding as a numpy array.

        Raises:
            ValueError: If the embedding dimension does not match the
                expected dimension.
        """
        vec = np.asarray(embedding, dtype=np.float64)
        if self._dimension is None:
            self._dimension = len(embedding)
        elif len(embedding) != self._dimension:
            msg = (
                f"Embedding dimension mismatch for fragment "
                f"'{fragment_id}': expected {self._dimension}, "
                f"got {len(embedding)}"
            )
            raise ValueError(msg)
        return vec

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

        Raises:
            ValueError: If the embedding dimension does not match
                previously added embeddings.
        """
        self._index[fragment_id] = self._validate_embedding(fragment_id, embedding)

    def add_batch(self, embeddings: dict[str, list[float]]) -> None:
        """Add multiple fragment embeddings to the index.

        Args:
            embeddings: Mapping of fragment IDs to embedding vectors.

        Raises:
            ValueError: If any embedding dimension does not match
                previously added embeddings.
        """
        for fid, emb in embeddings.items():
            self._index[fid] = self._validate_embedding(fid, emb)

    def clear(self) -> None:
        """Remove all fragments from the index and reset dimensionality."""
        self._index.clear()
        self._dimension = None

    def find_duplicates(
        self,
        embeddings: dict[str, list[float]],
    ) -> SemanticDuplicateResult:
        """Find near-duplicates and resonances in a batch of embeddings.

        Compares all unique pairs and classifies each by cosine
        similarity.  Does **not** modify the internal index.

        All vectors in *embeddings* must have the same dimensionality.

        Args:
            embeddings: Mapping of fragment IDs to embedding vectors.

        Returns:
            A :class:`SemanticDuplicateResult` with duplicate and
            resonance pairs sorted by similarity descending.

        Raises:
            ValueError: If embedding dimensions are inconsistent.
        """
        duplicates: list[SemanticDuplicatePair] = []
        resonances: list[SemanticDuplicatePair] = []

        ids = sorted(embeddings.keys())
        if not ids:
            return SemanticDuplicateResult(
                duplicates=duplicates,
                resonances=resonances,
            )

        # Convert to numpy and validate dimensions
        dim = len(embeddings[ids[0]])
        vecs: dict[str, np.ndarray] = {}
        for fid in ids:
            emb = embeddings[fid]
            if len(emb) != dim:
                msg = (
                    f"Embedding dimension mismatch for fragment "
                    f"'{fid}': expected {dim}, got {len(emb)}"
                )
                raise ValueError(msg)
            vecs[fid] = np.asarray(emb, dtype=np.float64)

        for i in range(len(ids)):
            for j in range(i + 1, len(ids)):
                sim = _cosine_similarity(vecs[ids[i]], vecs[ids[j]])
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

        vec = np.asarray(embedding, dtype=np.float64)
        for indexed_id, indexed_vec in self._index.items():
            sim = _cosine_similarity(vec, indexed_vec)
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
        # Clamp to [-1, 1] to handle floating-point rounding
        clamped = max(-1.0, min(1.0, similarity))

        if clamped >= self.duplicate_threshold:
            return SemanticDuplicatePair(
                fragment_id_a=id_a,
                fragment_id_b=id_b,
                similarity=clamped,
                classification="duplicate",
            )
        if clamped >= self.resonance_threshold:
            return SemanticDuplicatePair(
                fragment_id_a=id_a,
                fragment_id_b=id_b,
                similarity=clamped,
                classification="resonance",
            )
        return None
