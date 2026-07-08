"""Top-k nearest-neighbour resonance discovery.

The exact all-pairs path stays the default; ``resonance_method = "topk"`` keeps
only each fragment's ``resonance_top_k`` best above-threshold neighbours,
bounding the edge set to O(n·k). These tests use a deterministic angular fan of
unit vectors (no similarity ties) so each fragment's top-k is hand-verifiable,
plus a scale test proving the O(n·k) bound.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

import numpy as np
import pytest

from creek.config import EmbeddingsConfig
from creek.link.embeddings import EmbeddingLinker
from creek.models import Fragment, FragmentSource, SourcePlatform

if TYPE_CHECKING:
    from creek.models import FragmentLevel


def _fan(degrees: float) -> list[float]:
    """Return a 2-D unit vector at *degrees* from the x-axis."""
    radians = math.radians(degrees)
    return [math.cos(radians), math.sin(radians)]


def _hier_fragment(
    fid: str,
    *,
    parent_id: str | None = None,
    child_ids: list[str] | None = None,
    level: FragmentLevel = "document",
) -> Fragment:
    """Build a Fragment with explicit hierarchy fields for suppression tests."""
    return Fragment(
        id=fid,
        title=fid,
        source=FragmentSource(platform=SourcePlatform.CLAUDE),
        parent_id=parent_id,
        child_ids=child_ids or [],
        level=level,
    )


# A fan where cosine similarity = cos(angle gap). Gaps widen (4°,6°,8°) so there
# are no ties, making each fragment's ranked neighbours unambiguous. F4 is 90°
# away from everything, so it never resonates above a 0.95 threshold.
_FAN_EMBEDDINGS = {
    "F0": _fan(0),
    "F1": _fan(4),
    "F2": _fan(10),
    "F3": _fan(18),
    "F4": _fan(90),
}


def _pairs(
    linker: EmbeddingLinker, embeddings: dict[str, list[float]]
) -> list[tuple[str, str]]:
    """Return resonance edges as ``(a_id, b_id)`` tuples."""
    return [
        (r.fragment_a_id, r.fragment_b_id) for r in linker.find_resonances(embeddings)
    ]


def test_exact_is_the_default_method() -> None:
    """A fresh EmbeddingsConfig uses the exact all-pairs path."""
    assert EmbeddingsConfig().resonance_method == "exact"


def test_exact_emits_every_above_threshold_pair() -> None:
    """The exact path links all four near-axis fragments pairwise."""
    linker = EmbeddingLinker(config=EmbeddingsConfig(similarity_threshold=0.95))
    pairs = _pairs(linker, _FAN_EMBEDDINGS)
    assert pairs == [
        ("F0", "F1"),
        ("F0", "F2"),
        ("F0", "F3"),
        ("F1", "F2"),
        ("F1", "F3"),
        ("F2", "F3"),
    ]


def test_topk_k1_keeps_each_fragments_single_best_neighbour() -> None:
    """With k=1 every fragment keeps only its closest above-threshold neighbour."""
    linker = EmbeddingLinker(
        config=EmbeddingsConfig(
            similarity_threshold=0.95,
            resonance_method="topk",
            resonance_top_k=1,
        ),
    )
    # F0→F1, F1→F0, F2→F1, F3→F2 (each its nearest); deduped + (i<j)-ordered.
    assert _pairs(linker, _FAN_EMBEDDINGS) == [
        ("F0", "F1"),
        ("F1", "F2"),
        ("F2", "F3"),
    ]


def test_topk_output_is_a_subset_of_the_exact_path() -> None:
    """Top-k edges are a strict subset of the exact edges (the weakest dropped)."""
    exact = EmbeddingLinker(config=EmbeddingsConfig(similarity_threshold=0.95))
    topk = EmbeddingLinker(
        config=EmbeddingsConfig(
            similarity_threshold=0.95,
            resonance_method="topk",
            resonance_top_k=2,
        ),
    )
    exact_pairs = set(_pairs(exact, _FAN_EMBEDDINGS))
    topk_pairs = set(_pairs(topk, _FAN_EMBEDDINGS))

    assert topk_pairs <= exact_pairs
    assert len(topk_pairs) < len(exact_pairs)
    # k=2 drops only F0↔F3 (each prefers two closer neighbours).
    assert exact_pairs - topk_pairs == {("F0", "F3")}


def test_topk_preserves_ascending_index_order() -> None:
    """Top-k edges are emitted in the same ascending (i, j) order as exact."""
    linker = EmbeddingLinker(
        config=EmbeddingsConfig(
            similarity_threshold=0.95,
            resonance_method="topk",
            resonance_top_k=2,
        ),
    )
    pairs = _pairs(linker, _FAN_EMBEDDINGS)
    assert pairs == sorted(pairs)


def test_topk_suppresses_hierarchy_trivial_pairs() -> None:
    """A parent/child pair is never emitted by the top-k path either.

    Exercises the ``suppressed`` branch in ``_resonances_topk``: the two
    fragments are identical (cosine 1.0, so each is the other's top neighbour),
    but they are ancestor/descendant, so FEAT-024 suppression must drop the edge
    — matching the exact path.
    """
    linker = EmbeddingLinker(
        config=EmbeddingsConfig(
            similarity_threshold=0.5,
            resonance_method="topk",
            resonance_top_k=5,
        ),
    )
    fragments = {
        "p": _hier_fragment("p", child_ids=["c1"], level="document"),
        "c1": _hier_fragment("c1", parent_id="p", level="paragraph"),
    }
    embeddings = {"p": [1.0, 0.0], "c1": [1.0, 0.0]}

    assert linker.find_resonances(embeddings, fragments) == []


def test_topk_edge_count_is_bounded_small() -> None:
    """A CI-visible bound check: edges never exceed n·k (the core guarantee)."""
    n, k = 60, 3
    rng = np.random.default_rng(13)
    vectors = rng.standard_normal((n, 8)).astype(np.float32)
    embeddings = {f"f{i:03d}": vectors[i].tolist() for i in range(n)}

    linker = EmbeddingLinker(
        config=EmbeddingsConfig(
            # A wide-open gate: every non-negatively-similar pair qualifies, so
            # the top-k cap (not the threshold) is what bounds the output.
            similarity_threshold=0.0,
            resonance_method="topk",
            resonance_top_k=k,
        ),
    )
    resonances = linker.find_resonances(embeddings)

    assert 0 < len(resonances) <= n * k


@pytest.mark.slow
def test_topk_edge_count_is_bounded_at_scale() -> None:
    """Top-k caps the edge set at O(n·k) at a larger N than the routine gate.

    ``test_topk_edge_count_is_bounded_small`` is the always-on CI guard for the
    bound; this ``slow``-marked variant exercises a bigger N for extra
    confidence. The ``slow`` marker is descriptive — ``addopts`` does not
    auto-deselect it, and at n=800 it is still a sub-second numpy test.
    """
    n, k = 800, 5
    rng = np.random.default_rng(20260629)
    vectors = rng.standard_normal((n, 16)).astype(np.float32)
    embeddings = {f"f{i:04d}": vectors[i].tolist() for i in range(n)}

    linker = EmbeddingLinker(
        config=EmbeddingsConfig(
            # Wide-open gate (every non-negative-cosine pair qualifies) so exact
            # would be ~O(n²); only the top-k cap keeps the output bounded.
            similarity_threshold=0.0,
            resonance_method="topk",
            resonance_top_k=k,
        ),
    )
    resonances = linker.find_resonances(embeddings)

    # Each of n fragments contributes at most k directed picks; undirected
    # dedup only shrinks that, so the bound is n·k (vs ~n²/2 for exact).
    assert len(resonances) <= n * k
    assert len(resonances) > 0
