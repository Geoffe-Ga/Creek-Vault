"""Tests for creek.link.neighbours — vectorised cosine-neighbour discovery (#790).

The blocked-numpy neighbour search underpins eddy DBSCAN and paradox candidate
generation. These tests pin its contract: self-exclusion, symmetry, ascending
order, threshold semantics, zero-norm handling, block invariance, and that it
reproduces a brute-force reference on non-boundary vectors.
"""

from __future__ import annotations

import math

from creek.link.neighbours import cosine_neighbours


def _cosine(a: list[float], b: list[float]) -> float:
    """Reference cosine similarity (``0.0`` for a zero-norm vector)."""
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


def _brute_force(vectors: list[list[float]], threshold: float) -> list[list[int]]:
    """Exhaustive O(N²) reference the vectorised path must reproduce."""
    out: list[list[int]] = []
    for i, vec_i in enumerate(vectors):
        out.append(
            [
                j
                for j, vec_j in enumerate(vectors)
                if j != i and _cosine(vec_i, vec_j) >= threshold
            ],
        )
    return out


def test_empty_returns_empty() -> None:
    """No vectors yields no neighbour lists."""
    assert cosine_neighbours([], 0.5) == []


def test_single_vector_has_no_neighbours() -> None:
    """A lone vector cannot neighbour itself."""
    assert cosine_neighbours([[1.0, 0.0]], 0.0) == [[]]


def test_self_is_excluded() -> None:
    """Identical vectors neighbour each other but never themselves."""
    result = cosine_neighbours([[1.0, 0.0], [1.0, 0.0]], 0.9)
    assert result == [[1], [0]]


def test_orthogonal_vectors_are_not_neighbours() -> None:
    """Orthogonal unit vectors have cosine 0, below a positive threshold."""
    result = cosine_neighbours([[1.0, 0.0], [0.0, 1.0]], 0.5)
    assert result == [[], []]


def test_neighbours_are_ascending() -> None:
    """Each neighbour list is in ascending index order."""
    vectors = [[1.0, 0.0], [1.0, 0.0], [1.0, 0.0], [0.0, 1.0]]
    result = cosine_neighbours(vectors, 0.9)
    assert result[0] == [1, 2]
    assert result == [sorted(row) for row in result]


def test_symmetry() -> None:
    """``j in out[i]`` iff ``i in out[j]``."""
    vectors = [[1.0, 0.1], [0.9, 0.2], [0.0, 1.0], [0.1, 0.9]]
    result = cosine_neighbours(vectors, 0.6)
    for i, row in enumerate(result):
        for j in row:
            assert i in result[j]


def test_zero_norm_vector_never_neighbours() -> None:
    """A zero vector has cosine 0 with everything, so no positive-threshold hit."""
    vectors = [[0.0, 0.0], [1.0, 0.0], [1.0, 0.0]]
    result = cosine_neighbours(vectors, 0.5)
    assert result[0] == []
    assert 0 not in result[1]
    assert 0 not in result[2]


def test_block_size_is_invariant() -> None:
    """A tiny block height yields the same result as one big block."""
    vectors = [[float(i % 3 == k) for k in range(3)] for i in range(12)]
    small = cosine_neighbours(vectors, 0.5, block_rows=2)
    big = cosine_neighbours(vectors, 0.5, block_rows=1000)
    assert small == big


def test_matches_brute_force_reference() -> None:
    """Vectorised neighbours equal the exhaustive reference on non-boundary data."""
    vectors = [
        [1.0, 0.05, 0.0],
        [0.95, 0.1, 0.02],
        [0.0, 1.0, 0.1],
        [0.02, 0.9, 0.2],
        [0.1, 0.1, 1.0],
    ]
    threshold = 0.6
    assert cosine_neighbours(vectors, threshold) == _brute_force(vectors, threshold)
