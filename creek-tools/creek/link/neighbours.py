"""Vectorised cosine-neighbour discovery for density clustering (issue #790).

Eddy DBSCAN (:mod:`creek.link.eddies`) and paradox candidate generation
(:mod:`creek.generate.paradox`) both need, for every fragment, the other
fragments whose embedding cosine similarity clears a threshold. A pure-Python
double loop makes that O(N²) in interpreted code, which does not finish on a
~35k-fragment vault. This module mirrors the blocked-numpy approach used by
:meth:`creek.link.embeddings.EmbeddingLinker._resonances_topk`: it L2-normalises
the embedding matrix once, then computes cosine similarities one row-block at a
time (``block @ matrix.T``) so peak memory is ``block_rows x N`` rather than the
full ``N x N`` product. The neighbour sets returned are identical (up to
float32 rounding at the exact threshold) to the exhaustive pairwise computation.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final, cast

if TYPE_CHECKING:
    import numpy as np
    from numpy.typing import NDArray

NEIGHBOUR_BLOCK_ROWS: Final[int] = 512
"""Row-block height for the blocked similarity matmul (peak mem = block x N)."""


def _normalise(matrix: NDArray[np.float32]) -> NDArray[np.float32]:
    """Return *matrix* L2-normalised row-wise, clamping zero norms.

    Args:
        matrix: A ``(n, d)`` float32 embedding matrix.

    Returns:
        The row-normalised matrix. Zero-norm rows become near-zero vectors
        (the divisor is clamped to ``1e-10``) so their cosine similarity to
        anything is ``0.0`` — matching the pure-Python zero-norm convention.
    """
    import numpy as np  # lazy: numpy lives in the [embeddings] extra

    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    norms = np.maximum(norms, 1e-10)
    return cast("NDArray[np.float32]", matrix / norms)


def cosine_neighbours(
    vectors: list[list[float]],
    threshold: float,
    *,
    block_rows: int = NEIGHBOUR_BLOCK_ROWS,
) -> list[list[int]]:
    """Return, per row, the ascending indices with cosine similarity >= *threshold*.

    Each row's own index is excluded (self-similarity is ``1.0``). Similarities
    are computed one ``block_rows``-tall block at a time (``block @ matrix.T``)
    so peak memory is ``block_rows x n`` rather than the full ``n x n`` matrix.
    The result is symmetric: ``j in out[i]`` iff ``i in out[j]``.

    Args:
        vectors: Row-major embedding vectors (all of equal length).
        threshold: Minimum cosine similarity for a neighbour to qualify.
        block_rows: Number of rows per similarity block (positive).

    Returns:
        A list of length ``len(vectors)``; entry ``i`` is the ascending list of
        neighbour indices ``j != i`` with ``cosine(i, j) >= threshold``.
    """
    import numpy as np  # lazy: numpy lives in the [embeddings] extra

    n = len(vectors)
    if n == 0:
        return []
    matrix = _normalise(np.asarray(vectors, dtype=np.float32))
    neighbours: list[list[int]] = [[] for _ in range(n)]
    for start in range(0, n, block_rows):
        stop = min(start + block_rows, n)
        block_sims = matrix[start:stop] @ matrix.T
        for local_index, i in enumerate(range(start, stop)):
            hits = np.nonzero(block_sims[local_index] >= threshold)[0]
            neighbours[i] = [j for j in hits.tolist() if j != i]
    return neighbours
