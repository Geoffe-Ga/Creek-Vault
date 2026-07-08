"""Scale + equivalence tests for the #790 vectorisation of eddy DBSCAN.

Proves the vectorised neighbour build produces clusters identical to an
independent brute-force DBSCAN, that end-to-end eddy detection still recovers
planted clusters, and that detection completes on a multi-thousand-fragment
corpus (the pure-Python O(N²) path could not).
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta

from creek.link.eddies import EddyDetector
from creek.models import (
    Fragment,
    FragmentSource,
    Frequency,
    FrequencyClassification,
    Phase,
    SourcePlatform,
    WavelengthClassification,
)

_EPS = 0.3
_MIN_SAMPLES = 5


def _cos(a: list[float], b: list[float]) -> float:
    """Plain cosine similarity (``0.0`` when either vector has zero norm)."""
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


def _bf_dbscan(
    ids: list[str],
    embeddings: dict[str, list[float]],
    eps: float,
    min_samples: int,
) -> list[list[str]]:
    """Independent brute-force DBSCAN mirroring the production expand logic."""
    n = len(ids)
    unvisited, noise = -1, -2
    neigh = [
        [
            j
            for j in range(n)
            if j != i and (1.0 - _cos(embeddings[ids[i]], embeddings[ids[j]])) <= eps
        ]
        for i in range(n)
    ]
    labels = [unvisited] * n
    cluster_id = 0
    for i in range(n):
        if labels[i] != unvisited:
            continue
        if len(neigh[i]) + 1 < min_samples:
            labels[i] = noise
            continue
        labels[i] = cluster_id
        queue = list(neigh[i])
        while queue:
            cur = queue.pop()
            if labels[cur] == noise:
                labels[cur] = cluster_id
                continue
            if labels[cur] != unvisited:
                continue
            labels[cur] = cluster_id
            if len(neigh[cur]) + 1 >= min_samples:
                queue.extend(neigh[cur])
        cluster_id += 1
    clusters: dict[int, list[str]] = {}
    for idx, label in enumerate(labels):
        if label >= 0:
            clusters.setdefault(label, []).append(ids[idx])
    return [sorted(members) for members in clusters.values()]


def _normalise_clusters(clusters: list[list[str]]) -> set[frozenset[str]]:
    """Represent a clustering as an order-independent set of member sets."""
    return {frozenset(members) for members in clusters}


def _fragment(fid: str, created: datetime) -> Fragment:
    """Build a minimal embedded-capable fragment with a fixed id and time."""
    return Fragment(
        id=fid,
        title=f"frag {fid}",
        source=FragmentSource(platform=SourcePlatform.CLAUDE),
        created=created,
        ingested=created,
        frequency=FrequencyClassification(primary=Frequency.UNCLASSIFIED),
        wavelength=WavelengthClassification(phase=Phase.UNCLASSIFIED),
    )


def _clustered_corpus(
    clusters: int,
    per_cluster: int,
) -> tuple[list[Fragment], dict[str, list[float]]]:
    """Return fragments with a distinct one-hot embedding per cluster.

    Each cluster owns a unique dimension, so within-cluster similarity is
    ``1.0`` (dense DBSCAN cluster) and cross-cluster similarity is ``0.0``
    (well separated, no boundary ties). Identical within-cluster vectors give
    zero content drift, so the Spearman time-vs-drift correlation is ``0`` and
    every cluster qualifies as an eddy rather than a thread-like progression.
    """
    dims = clusters + 1
    base = datetime(2024, 1, 1)
    fragments: list[Fragment] = []
    embeddings: dict[str, list[float]] = {}
    for cluster in range(clusters):
        vector = [0.0] * dims
        vector[cluster] = 1.0
        for member in range(per_cluster):
            fid = f"frag-{cluster:02d}-{member:03d}"
            created = base + timedelta(days=cluster * per_cluster + member)
            fragments.append(_fragment(fid, created))
            embeddings[fid] = list(vector)
    return fragments, embeddings


def test_dbscan_matches_brute_force_reference() -> None:
    """Vectorised _dbscan clusters equal an independent brute-force DBSCAN."""
    fragments, embeddings = _clustered_corpus(clusters=3, per_cluster=6)
    detector = EddyDetector(
        embeddings=embeddings,
        eps=_EPS,
        min_samples=_MIN_SAMPLES,
    )
    ids = [f.id for f in fragments]
    # Issue #790: access the private _dbscan to prove neighbour-build equivalence.
    produced = detector._dbscan(ids)
    expected = _bf_dbscan(ids, embeddings, _EPS, _MIN_SAMPLES)
    assert _normalise_clusters(produced) == _normalise_clusters(expected)


def test_detect_eddies_recovers_planted_clusters() -> None:
    """End-to-end eddy detection recovers the planted dense clusters."""
    fragments, embeddings = _clustered_corpus(clusters=2, per_cluster=6)
    detector = EddyDetector(
        embeddings=embeddings,
        eps=_EPS,
        min_samples=_MIN_SAMPLES,
    )
    eddies = detector.detect_eddies(fragments, min_fragments=_MIN_SAMPLES)
    memberships = _normalise_clusters(list(detector.eddy_members.values()))
    assert len(eddies) == 2
    assert memberships == {
        frozenset(f.id for f in fragments[:6]),
        frozenset(f.id for f in fragments[6:]),
    }


def test_detect_eddies_scales_to_thousands() -> None:
    """Detection completes on ~2400 fragments (the O(N²) path could not)."""
    fragments, embeddings = _clustered_corpus(clusters=40, per_cluster=60)
    detector = EddyDetector(
        embeddings=embeddings,
        eps=_EPS,
        min_samples=_MIN_SAMPLES,
    )
    eddies = detector.detect_eddies(fragments, min_fragments=_MIN_SAMPLES)
    # 40 well-separated dense clusters of 60 → 40 eddies.
    assert len(eddies) == 40
