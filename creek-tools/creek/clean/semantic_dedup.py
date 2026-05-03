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

The batch path uses a vectorised matmul (``X @ X.T`` after L2
normalisation) so a 10 000-fragment vault is dominated by BLAS, not Python
loops. A FAISS-backed approximate-nearest-neighbour fallback is available
behind :class:`DeduplicationConfig.use_faiss`; FAISS is an optional
dependency and the code degrades gracefully to the dense matmul when it
is not installed.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    import numpy as np
    from numpy.typing import NDArray

    FloatArray = NDArray[np.float32]


logger = logging.getLogger(__name__)


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
    import numpy as np  # lazy: numpy lives in the [embeddings] extra

    norm_a = np.linalg.norm(vec_a)
    norm_b = np.linalg.norm(vec_b)

    if 0.0 in (norm_a, norm_b):
        return 0.0

    return float(np.dot(vec_a, vec_b) / (norm_a * norm_b))


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


class DeduplicationConfig(BaseModel):
    """Tunable knobs for :class:`SemanticDeduplicator`.

    Threshold ranges are enforced inside :class:`SemanticDeduplicator`'s
    constructor (not by Pydantic) so the validation error message stays
    consistent across the keyword-argument and config-object code paths.

    Attributes:
        duplicate_threshold: Cosine similarity at or above which a pair
            is classified as a duplicate.
        resonance_threshold: Cosine similarity at or above which (but
            below ``duplicate_threshold``) a pair is classified as a
            resonance.
        use_faiss: When ``True`` and the optional ``faiss`` package is
            installed, use an approximate-nearest-neighbour index for
            the batch all-pairs query. Falls back to the dense matmul
            transparently when ``faiss`` is unavailable so this knob
            never becomes a hard dependency.
    """

    duplicate_threshold: float = Field(default=0.95)
    resonance_threshold: float = Field(default=0.75)
    use_faiss: bool = False


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
# Vectorised similarity helpers
# ---------------------------------------------------------------------------


def _l2_normalise(matrix: FloatArray) -> FloatArray:
    """Return ``matrix`` row-normalised to unit length.

    Zero rows are preserved as zero rows (rather than producing ``NaN``)
    so a zero embedding contributes zero similarity to every pair.

    Args:
        matrix: 2-D array of shape ``(n, d)``.

    Returns:
        A new array of the same shape with each non-zero row scaled to
        unit L2 norm.
    """
    import numpy as np

    norms = np.linalg.norm(matrix, axis=1, keepdims=True).astype(np.float32)
    safe = np.where(norms == 0.0, np.float32(1.0), norms)
    normalised: FloatArray = (matrix / safe).astype(np.float32, copy=False)
    return normalised


def _stack_embeddings(
    ids: list[str],
    embeddings: dict[str, list[float]],
    expected_dim: int,
) -> FloatArray:
    """Validate dimensions and stack *ids* into a contiguous float32 matrix."""
    import numpy as np

    for fid in ids:
        if len(embeddings[fid]) != expected_dim:
            msg = (
                f"Embedding dimension mismatch for fragment "
                f"'{fid}': expected {expected_dim}, "
                f"got {len(embeddings[fid])}"
            )
            raise ValueError(msg)
    matrix: FloatArray = np.asarray(
        [embeddings[fid] for fid in ids],
        dtype=np.float32,
    )
    return matrix


def _faiss_pair_indices(
    matrix: FloatArray,
    threshold: float,
) -> tuple[NDArray[np.int64], NDArray[np.int64], NDArray[np.float32]]:
    """Return upper-triangle (i, j, sim) triples above *threshold* via FAISS.

    Caller is responsible for ensuring ``faiss`` is importable and that
    *matrix* has been L2-normalised. Falls back to ``range_search`` on
    ``IndexFlatIP`` so an exact result is returned (useful when the
    pure-Python matmul would exceed the memory budget).
    """
    import faiss  # type: ignore[import-not-found]
    import numpy as np

    n, dim = matrix.shape
    index = faiss.IndexFlatIP(dim)
    index.add(matrix)
    lims, distances, neighbours = index.range_search(matrix, float(threshold))
    rows_list: list[np.ndarray] = [
        np.full(lims[i + 1] - lims[i], i, dtype=np.int64) for i in range(n)
    ]
    rows = np.concatenate(rows_list) if rows_list else np.empty(0, dtype=np.int64)
    cols = neighbours.astype(np.int64)
    sims = distances.astype(np.float32)
    upper = cols > rows
    return rows[upper], cols[upper], sims[upper]


def _matmul_pair_indices(
    matrix: FloatArray,
    threshold: float,
) -> tuple[NDArray[np.int64], NDArray[np.int64], NDArray[np.float32]]:
    """Return upper-triangle (i, j, sim) triples above *threshold* via matmul.

    Caller is responsible for ensuring *matrix* has been L2-normalised.

    Memory: only **one** ``N x N`` float32 matrix is live at a time
    (≈ 400 MB at N = 10 000). The previous implementation used
    ``np.triu`` which materialised a second N x N copy alongside the
    matmul result, doubling peak memory; the current code uses
    ``np.triu_indices`` to extract the strictly upper-triangle
    coordinates without copying the matrix.

    The documented FAISS path is the right cutover for larger corpora.
    """
    import numpy as np

    sim = matrix @ matrix.T
    rows, cols = np.triu_indices(sim.shape[0], k=1)
    upper = sim[rows, cols]
    mask = upper >= threshold
    return (
        rows[mask].astype(np.int64, copy=False),
        cols[mask].astype(np.int64, copy=False),
        upper[mask].astype(np.float32, copy=False),
    )


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

    The batch path uses a vectorised matmul (``X @ X.T``) so 10 000
    embeddings finish in a few seconds rather than minutes. Pass
    ``config=DeduplicationConfig(use_faiss=True)`` to opt into the FAISS
    fallback for very large corpora; FAISS is optional and the code
    falls back to the matmul if the package is missing.

    Attributes:
        duplicate_threshold: Minimum cosine similarity for a pair to be
            classified as a near-duplicate.
        resonance_threshold: Minimum cosine similarity for a pair to be
            classified as a resonance (must be below
            ``duplicate_threshold``).
        config: The :class:`DeduplicationConfig` driving classification
            and the choice of similarity backend.
    """

    def __init__(
        self,
        *,
        duplicate_threshold: float = 0.95,
        resonance_threshold: float = 0.75,
        config: DeduplicationConfig | None = None,
    ) -> None:
        """Initialise the deduplicator with configurable thresholds.

        Args:
            duplicate_threshold: Cosine similarity at or above which a
                pair is classified as a near-duplicate. Ignored when
                *config* is provided.
            resonance_threshold: Cosine similarity at or above which
                (but below ``duplicate_threshold``) a pair is classified
                as a resonance. Ignored when *config* is provided.
            config: Optional :class:`DeduplicationConfig`. When provided,
                its thresholds and ``use_faiss`` flag take precedence
                over the standalone keyword arguments.

        Raises:
            ValueError: If ``resonance_threshold`` is not less than
                ``duplicate_threshold``, or if either is outside (0, 1].
        """
        if config is None:
            config = DeduplicationConfig(
                duplicate_threshold=duplicate_threshold,
                resonance_threshold=resonance_threshold,
            )
        if not (0.0 < config.resonance_threshold < config.duplicate_threshold <= 1.0):
            msg = (
                f"resonance_threshold ({config.resonance_threshold}) must be "
                f"less than duplicate_threshold ({config.duplicate_threshold}) "
                f"and both must be in (0, 1]"
            )
            raise ValueError(msg)

        self.config = config
        self.duplicate_threshold = config.duplicate_threshold
        self.resonance_threshold = config.resonance_threshold
        self._index: dict[str, np.ndarray] = {}
        self._dimension: int | None = None
        # Memoised result of the ``import faiss`` probe in
        # ``_faiss_available``. ``None`` = not yet probed; ``True`` /
        # ``False`` = importable / not.
        self._faiss_probe_cache: bool | None = None

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
        import numpy as np  # lazy: numpy lives in the [embeddings] extra

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

        Implementation: stacks the embeddings into an ``(N, D)`` float32
        matrix, L2-normalises rows, and computes the upper-triangle of
        ``X @ X.T`` in a single BLAS call. Pairs with similarity below
        ``resonance_threshold`` are filtered out before any Python-level
        iteration so a 10 000-fragment input that previously needed
        minutes now finishes in seconds.

        Args:
            embeddings: Mapping of fragment IDs to embedding vectors.

        Returns:
            A :class:`SemanticDuplicateResult` with duplicate and
            resonance pairs sorted by similarity descending.

        Raises:
            ValueError: If embedding dimensions are inconsistent.
        """
        ids = sorted(embeddings.keys())
        if len(ids) < 2:
            return SemanticDuplicateResult(duplicates=[], resonances=[])

        dim = len(embeddings[ids[0]])
        matrix = _stack_embeddings(ids, embeddings, dim)
        normalised = _l2_normalise(matrix)

        rows, cols, sims = self._pair_indices(normalised)

        duplicates: list[SemanticDuplicatePair] = []
        resonances: list[SemanticDuplicatePair] = []
        for i, j, sim in zip(rows, cols, sims, strict=True):
            pair = self._classify_pair(ids[int(i)], ids[int(j)], float(sim))
            if pair is None:
                continue
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

    def _pair_indices(
        self,
        normalised: FloatArray,
    ) -> tuple[NDArray[np.int64], NDArray[np.int64], NDArray[np.float32]]:
        """Return ``(rows, cols, sims)`` for pairs above resonance threshold.

        Honors :attr:`DeduplicationConfig.use_faiss` and gracefully
        degrades to the dense matmul only if the top-level ``faiss``
        import itself fails. ``ImportError`` raised from inside FAISS
        (e.g. a missing submodule) is **not** swallowed so internal
        FAISS bugs surface as real failures rather than silent
        fallbacks.
        """
        if self.config.use_faiss and self._faiss_available():
            return _faiss_pair_indices(normalised, self.resonance_threshold)
        return _matmul_pair_indices(normalised, self.resonance_threshold)

    def _faiss_available(self) -> bool:
        """Return whether the optional ``faiss`` package is importable.

        The probe result is cached on the instance so a long-running
        pipeline that calls :meth:`find_duplicates` repeatedly with
        ``use_faiss=True`` against a FAISS-less environment emits at
        most one fallback warning per deduplicator instead of one per
        call.
        """
        cached = self._faiss_probe_cache
        if cached is not None:
            return cached
        try:
            import faiss  # noqa: F401
        except ImportError:
            logger.warning(
                "use_faiss=True but faiss is not installed — "
                "falling back to dense matmul.",
            )
            self._faiss_probe_cache = False
            return False
        self._faiss_probe_cache = True
        return True

    def check_fragment(
        self,
        fragment_id: str,
        embedding: list[float],
    ) -> SemanticDuplicateResult:
        """Check a new fragment against the existing index (incremental mode).

        Compares the given embedding against all indexed fragments
        without modifying the index.  Use :meth:`add_fragment` to add
        the fragment to the index after reviewing results.

        Implementation: stacks the indexed embeddings into a single
        matrix, normalises both sides, and runs a single matmul so the
        per-call cost is proportional to a BLAS GEMV rather than ``N``
        Python-level cosine calls.

        Args:
            fragment_id: ID of the fragment to check.
            embedding: Pre-computed embedding vector for the fragment.

        Returns:
            A :class:`SemanticDuplicateResult` with matches found in
            the index.

        Raises:
            ValueError: If the embedding dimension does not match the
                index dimension.
        """
        import numpy as np  # lazy: numpy lives in the [embeddings] extra

        if self._dimension is not None and len(embedding) != self._dimension:
            msg = (
                f"Embedding dimension mismatch for fragment "
                f"'{fragment_id}': expected {self._dimension}, "
                f"got {len(embedding)}"
            )
            raise ValueError(msg)

        if not self._index:
            return SemanticDuplicateResult(duplicates=[], resonances=[])

        ids = list(self._index.keys())
        indexed_matrix: FloatArray = np.asarray(
            [self._index[fid] for fid in ids],
            dtype=np.float32,
        )
        query: FloatArray = np.asarray(embedding, dtype=np.float32).reshape(1, -1)
        sims = (_l2_normalise(query) @ _l2_normalise(indexed_matrix).T).reshape(-1)

        duplicates: list[SemanticDuplicatePair] = []
        resonances: list[SemanticDuplicatePair] = []
        for indexed_id, sim_value in zip(ids, sims, strict=True):
            pair = self._classify_pair(fragment_id, indexed_id, float(sim_value))
            if pair is None:
                continue
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
