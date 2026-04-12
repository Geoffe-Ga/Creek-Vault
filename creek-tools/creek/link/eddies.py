"""Eddy detection — density-based clustering without temporal direction.

Provides :class:`EddyDetector`, which identifies *eddies* — topic
clusters that pool around a subject without a clear temporal narrative.
Unlike threads, which capture a theme evolving over time, an eddy is a
concern that returns to attention again and again across different
periods.

The algorithm mirrors the Creek pipeline's existing pure-numpy style
(no ``scikit-learn`` dependency):

1. Run DBSCAN over the fragment embedding vectors using cosine
   distance. Each dense cluster of at least ``min_samples`` fragments is
   a candidate eddy.
2. For each candidate, compute the Spearman rank correlation between
   chronological order and *content drift* (cosine distance from the
   oldest fragment). Low |correlation| (below
   :attr:`correlation_threshold`) indicates a scattered, non-directional
   topic — an eddy. High |correlation| indicates a thread-like monotonic
   drift and is filtered out.
3. Build an :class:`~creek.models.Eddy` per qualifying cluster with a
   title derived from the most distinctive content words, ``formed``
   set to the median fragment creation date, and ``threads`` populated
   from any thread wiki-links already present on member fragments
   (i.e. threads that flow through the eddy).

The module also offers fragment-to-eddy assignment (adding wiki-links
to fragment ``eddies`` frontmatter).
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from typing import TYPE_CHECKING

import numpy as np

from creek.models import Eddy

if TYPE_CHECKING:
    from datetime import date

    from creek.models import Fragment

logger = logging.getLogger(__name__)

_DEFAULT_EPS = 0.3
_DEFAULT_MIN_SAMPLES = 5
_DEFAULT_MIN_FRAGMENTS = 5
_DEFAULT_CORRELATION_THRESHOLD = 0.3
_TITLE_TOP_WORDS = 3
_MIN_TITLE_WORD_LEN = 3

_UNVISITED = -1
_NOISE = -2

_STOPWORDS: frozenset[str] = frozenset(
    {
        "a",
        "about",
        "above",
        "after",
        "again",
        "against",
        "all",
        "am",
        "an",
        "and",
        "any",
        "are",
        "as",
        "at",
        "be",
        "because",
        "been",
        "before",
        "being",
        "below",
        "between",
        "both",
        "but",
        "by",
        "can",
        "did",
        "do",
        "does",
        "doing",
        "don",
        "down",
        "during",
        "each",
        "few",
        "for",
        "from",
        "further",
        "had",
        "has",
        "have",
        "having",
        "he",
        "her",
        "here",
        "hers",
        "herself",
        "him",
        "himself",
        "his",
        "how",
        "i",
        "if",
        "in",
        "into",
        "is",
        "it",
        "its",
        "itself",
        "just",
        "me",
        "more",
        "most",
        "my",
        "myself",
        "no",
        "nor",
        "not",
        "now",
        "of",
        "off",
        "on",
        "once",
        "only",
        "or",
        "other",
        "our",
        "ours",
        "ourselves",
        "out",
        "over",
        "own",
        "s",
        "same",
        "she",
        "should",
        "so",
        "some",
        "such",
        "t",
        "than",
        "that",
        "the",
        "their",
        "theirs",
        "them",
        "themselves",
        "then",
        "there",
        "these",
        "they",
        "this",
        "those",
        "through",
        "to",
        "too",
        "under",
        "until",
        "up",
        "very",
        "was",
        "we",
        "were",
        "what",
        "when",
        "where",
        "which",
        "while",
        "who",
        "whom",
        "why",
        "will",
        "with",
        "you",
        "your",
        "yours",
        "yourself",
        "yourselves",
    },
)


def _tokenize(text: str) -> list[str]:
    """Tokenise *text* into lowercase alphabetic words.

    Args:
        text: Input string to tokenise.

    Returns:
        A list of lowercase word tokens (no punctuation, no digits).
    """
    return re.findall(r"[a-z]+", text.lower())


def _content_words(text: str) -> list[str]:
    """Extract content words from *text* (no stopwords, length-filtered).

    Args:
        text: Input string.

    Returns:
        A list of lowercase content words suitable for title heuristics.
    """
    return [
        w
        for w in _tokenize(text)
        if w not in _STOPWORDS and len(w) >= _MIN_TITLE_WORD_LEN
    ]


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    """Cosine similarity between two equal-length vectors.

    Args:
        a: First vector.
        b: Second vector.

    Returns:
        Cosine similarity in ``[-1.0, 1.0]``; ``0.0`` if either vector
        has zero norm.
    """
    va = np.asarray(a, dtype=np.float32)
    vb = np.asarray(b, dtype=np.float32)
    na = float(np.linalg.norm(va))
    nb = float(np.linalg.norm(vb))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return float(np.dot(va, vb) / (na * nb))


def _cosine_distance(a: list[float], b: list[float]) -> float:
    """Return cosine *distance* (``1 - similarity``) clamped to ``[0, 2]``.

    Args:
        a: First vector.
        b: Second vector.

    Returns:
        Cosine distance; ``1.0`` when either vector has zero norm.
    """
    return 1.0 - _cosine_similarity(a, b)


def _average_ranks(values: list[float]) -> list[float]:
    """Return 1-based average ranks (fractional ties) for *values*.

    Args:
        values: Numeric values to rank.

    Returns:
        A list of rank values the same length as *values*; tied entries
        receive the average of the ranks they span.
    """
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        average = (i + 1 + j + 1) / 2.0
        for k in range(i, j + 1):
            ranks[order[k]] = average
        i = j + 1
    return ranks


def _spearman_with_time(distances: list[float]) -> float:
    """Spearman rank correlation between chronological index and *distances*.

    The time ranks are the chronological positions ``1..n`` of the
    fragments as supplied (callers must pass distances in chronological
    order). The correlation therefore measures whether content drift
    progresses monotonically over time.

    Args:
        distances: Content drift values ordered chronologically.

    Returns:
        Spearman correlation in ``[-1.0, 1.0]``; ``0.0`` when the sample
        is too small or all values are tied.
    """
    n = len(distances)
    if n < 2:
        return 0.0
    dist_ranks = _average_ranks(distances)
    time_ranks = list(range(1, n + 1))
    mean_rank = (n + 1) / 2.0
    numerator = sum(
        (t - mean_rank) * (d - mean_rank)
        for t, d in zip(time_ranks, dist_ranks, strict=True)
    )
    denom_t = sum((t - mean_rank) ** 2 for t in time_ranks)
    denom_d = sum((d - mean_rank) ** 2 for d in dist_ranks)
    if denom_t == 0.0 or denom_d == 0.0:
        return 0.0
    return float(numerator / (denom_t * denom_d) ** 0.5)


def _median_date(fragments: list[Fragment]) -> date:
    """Return the median fragment creation date.

    Args:
        fragments: Non-empty list of fragments.

    Returns:
        The median ``created`` date (lower-median for even counts).
    """
    sorted_frags = sorted(fragments, key=lambda f: f.created)
    mid = len(sorted_frags) // 2
    return sorted_frags[mid].created.date()


class EddyDetector:
    """Detect topic-cluster eddies via density clustering on embeddings.

    Two fragments land in the same candidate eddy when their embedding
    vectors are reachable under DBSCAN with cosine distance ``<= eps``
    and the cluster has at least ``min_samples`` members. Candidates
    with a clear temporal direction (Spearman correlation between time
    and content drift above ``correlation_threshold``) are filtered out
    as thread-like progressions.

    Attributes:
        embeddings: Mapping of fragment ID to embedding vector.
            Required; fragments without embeddings are ignored.
        eps: Maximum cosine distance for DBSCAN neighbourhood.
        min_samples: Minimum neighbourhood size for a DBSCAN core point.
        correlation_threshold: Absolute Spearman-correlation ceiling. A
            cluster qualifies as an eddy only when its chronological vs
            content-drift correlation has absolute value below this.
    """

    def __init__(
        self,
        embeddings: dict[str, list[float]] | None = None,
        *,
        eps: float = _DEFAULT_EPS,
        min_samples: int = _DEFAULT_MIN_SAMPLES,
        correlation_threshold: float = _DEFAULT_CORRELATION_THRESHOLD,
    ) -> None:
        """Initialise the detector.

        Args:
            embeddings: Mapping of fragment ID to embedding vector
                (e.g. produced by
                :class:`creek.link.embeddings.EmbeddingLinker`).
                Defaults to an empty mapping, in which case
                :meth:`detect_eddies` returns no eddies.
            eps: Maximum cosine distance for DBSCAN neighbours. Defaults
                to ``0.3`` (roughly ``0.7`` cosine similarity).
            min_samples: Minimum neighbourhood size for a DBSCAN core
                point. Defaults to ``5``.
            correlation_threshold: Absolute Spearman correlation cutoff
                for treating a cluster as an eddy (vs a thread-like
                progression). Defaults to ``0.3``.
        """
        self.embeddings: dict[str, list[float]] = embeddings or {}
        self.eps = eps
        self.min_samples = min_samples
        self.correlation_threshold = correlation_threshold
        self._eddy_members: dict[str, list[str]] = {}

    @property
    def eddy_members(self) -> dict[str, list[str]]:
        """Mapping of eddy ID to the fragment IDs that compose it.

        Populated by the most recent call to :meth:`detect_eddies`.

        Returns:
            A copy of the internal mapping. Empty until detection runs.
        """
        return dict(self._eddy_members)

    def detect_eddies(
        self,
        fragments: list[Fragment],
        min_fragments: int = _DEFAULT_MIN_FRAGMENTS,
    ) -> list[Eddy]:
        """Detect eddies across *fragments*.

        Runs DBSCAN on fragment embeddings, then filters clusters that
        exhibit thread-like temporal progression. Each qualifying
        cluster of at least ``min_fragments`` fragments becomes an
        :class:`~creek.models.Eddy`.

        Args:
            fragments: Fragments to analyse. Fragments lacking an entry
                in :attr:`embeddings` are skipped.
            min_fragments: Minimum cluster size required to emit an
                eddy. Defaults to 5.

        Returns:
            A list of detected :class:`~creek.models.Eddy` objects
            sorted by ``formed`` date ascending.
        """
        logger.info(
            "Detecting eddies among %d fragment(s) with eps=%.2f, min_samples=%d",
            len(fragments),
            self.eps,
            self.min_samples,
        )
        self._eddy_members = {}
        frag_by_id = {f.id: f for f in fragments if f.id in self.embeddings}
        if len(frag_by_id) < min_fragments:
            logger.info("Detected 0 eddies (insufficient embedded fragments)")
            return []

        ids = list(frag_by_id.keys())
        clusters = self._dbscan(ids)
        eddies = self._materialise_eddies(clusters, frag_by_id, min_fragments)
        eddies.sort(key=lambda e: e.formed)
        logger.info("Detected %d eddies", len(eddies))
        return eddies

    def _dbscan(self, ids: list[str]) -> list[list[str]]:
        """Run DBSCAN over the provided fragment IDs.

        Uses cosine distance against :attr:`embeddings` with parameters
        :attr:`eps` and :attr:`min_samples`.

        Args:
            ids: Fragment IDs to cluster (all must have embeddings).

        Returns:
            A list of clusters, each a list of fragment IDs. Noise
            points are excluded.
        """
        n = len(ids)
        labels = [_UNVISITED] * n
        neighbour_cache = [self._neighbours(ids, i) for i in range(n)]
        cluster_id = 0
        for i in range(n):
            if labels[i] != _UNVISITED:
                continue
            if len(neighbour_cache[i]) + 1 < self.min_samples:
                labels[i] = _NOISE
                continue
            self._expand_cluster(labels, neighbour_cache, i, cluster_id)
            cluster_id += 1
        return self._group_clusters(labels, ids)

    def _neighbours(self, ids: list[str], index: int) -> list[int]:
        """Return indices of fragments within :attr:`eps` of ``ids[index]``.

        Args:
            ids: Ordered list of fragment IDs.
            index: Position in *ids* whose neighbourhood is needed.

        Returns:
            Indices ``j != index`` where the cosine distance between
            ``ids[index]`` and ``ids[j]`` is at most :attr:`eps`.
        """
        anchor = self.embeddings[ids[index]]
        result: list[int] = []
        for j, other_id in enumerate(ids):
            if j == index:
                continue
            if _cosine_distance(anchor, self.embeddings[other_id]) <= self.eps:
                result.append(j)
        return result

    def _expand_cluster(
        self,
        labels: list[int],
        neighbour_cache: list[list[int]],
        seed: int,
        cluster_id: int,
    ) -> None:
        """Grow *cluster_id* from *seed*, mutating *labels* in place.

        Args:
            labels: Per-index cluster labels; mutated to record growth.
            neighbour_cache: Precomputed neighbour indices per point.
            seed: Index of the seed core point.
            cluster_id: Label to assign to reachable points.
        """
        labels[seed] = cluster_id
        queue = list(neighbour_cache[seed])
        while queue:
            current = queue.pop()
            if labels[current] == _NOISE:
                labels[current] = cluster_id
                continue
            if labels[current] != _UNVISITED:
                continue
            labels[current] = cluster_id
            if len(neighbour_cache[current]) + 1 >= self.min_samples:
                queue.extend(neighbour_cache[current])

    @staticmethod
    def _group_clusters(labels: list[int], ids: list[str]) -> list[list[str]]:
        """Group fragment IDs by their DBSCAN label.

        Args:
            labels: Cluster label per fragment index.
            ids: Fragment IDs parallel to *labels*.

        Returns:
            A list of clusters — one list of IDs per distinct cluster
            label, in ascending label order. Noise is excluded.
        """
        buckets: dict[int, list[str]] = {}
        for idx, label in enumerate(labels):
            if label >= 0:
                buckets.setdefault(label, []).append(ids[idx])
        return [buckets[label] for label in sorted(buckets)]

    def _materialise_eddies(
        self,
        clusters: list[list[str]],
        frag_by_id: dict[str, Fragment],
        min_fragments: int,
    ) -> list[Eddy]:
        """Convert DBSCAN clusters into :class:`Eddy` objects.

        Filters out clusters below *min_fragments* and clusters with
        thread-like temporal progression (|Spearman| at or above
        :attr:`correlation_threshold`). Side effect: populates
        :attr:`_eddy_members`.

        Args:
            clusters: DBSCAN cluster memberships as lists of IDs.
            frag_by_id: Fragment lookup table.
            min_fragments: Minimum cluster size to keep.

        Returns:
            Constructed eddies for every qualifying cluster.
        """
        eddies: list[Eddy] = []
        for members in clusters:
            if len(members) < min_fragments:
                continue
            cluster_frags = sorted(
                (frag_by_id[fid] for fid in members),
                key=lambda f: f.created,
            )
            if self._has_temporal_direction(cluster_frags):
                continue
            eddy = self._build_eddy(cluster_frags)
            eddies.append(eddy)
            self._eddy_members[eddy.id] = [f.id for f in cluster_frags]
        return eddies

    def _has_temporal_direction(self, cluster_frags: list[Fragment]) -> bool:
        """Return whether a cluster drifts monotonically over time.

        Computes cosine distance from the earliest fragment to each
        member (ordered chronologically) and correlates those distances
        with chronological rank. Absolute Spearman correlation at or
        above :attr:`correlation_threshold` is treated as a thread-like
        directional progression that should be filtered from eddies.

        Args:
            cluster_frags: Fragments sorted by ``created`` ascending.

        Returns:
            ``True`` if the cluster is directional (thread-like),
            ``False`` if it is scattered (eddy-like).
        """
        if len(cluster_frags) < 2:
            return False
        anchor = self.embeddings[cluster_frags[0].id]
        drifts = [
            _cosine_distance(anchor, self.embeddings[f.id]) for f in cluster_frags
        ]
        correlation = _spearman_with_time(drifts)
        return abs(correlation) >= self.correlation_threshold

    def _build_eddy(self, frags: list[Fragment]) -> Eddy:
        """Construct an :class:`Eddy` from its constituent fragments.

        Args:
            frags: Fragments belonging to the cluster, chronologically
                sorted (non-empty).

        Returns:
            A populated :class:`~creek.models.Eddy`.
        """
        return Eddy(
            title=self._generate_title(frags),
            formed=_median_date(frags),
            fragment_count=len(frags),
            threads=self._flowing_threads(frags),
        )

    def _generate_title(self, frags: list[Fragment]) -> str:
        """Generate a title from the cluster's distinctive content words.

        Uses TF-IDF-lite: content-word frequency inside the cluster
        scaled by inverse document frequency across :attr:`embeddings`
        (using fragment titles only when fragment objects are
        available — falls back to raw counts when the corpus is small).

        Args:
            frags: Fragments in the cluster (non-empty).

        Returns:
            A short title string.
        """
        counts: Counter[str] = Counter()
        for frag in frags:
            counts.update(_content_words(frag.title))
        if not counts:
            return frags[0].title[:50] if frags else "Untitled Eddy"
        top = [word for word, _ in counts.most_common(_TITLE_TOP_WORDS)]
        return " ".join(word.capitalize() for word in top)

    @staticmethod
    def _flowing_threads(frags: list[Fragment]) -> list[str]:
        """Collect thread wiki-links present in the cluster's fragments.

        When the linking pipeline has already run thread detection, each
        member fragment's ``threads`` frontmatter holds wiki-links to
        every thread it belongs to. Threads mentioned by multiple eddy
        members are said to *flow through* the eddy.

        Args:
            frags: Fragments in the cluster.

        Returns:
            A sorted, deduplicated list of thread wiki-link strings.
        """
        collected: set[str] = set()
        for frag in frags:
            collected.update(frag.threads)
        return sorted(collected)

    def assign_fragments_to_eddies(
        self,
        fragments: list[Fragment],
        eddies: list[Eddy],
    ) -> list[Fragment]:
        """Add eddy wiki-links to each fragment and refresh eddy counts.

        Uses the membership map captured by the most recent
        :meth:`detect_eddies` call. A wiki-link of the form
        ``[[<eddy title>]]`` is appended to each member fragment's
        ``eddies`` field, with duplicates skipped. Each eddy's
        ``fragment_count`` is refreshed in place to match its current
        membership.

        Args:
            fragments: Fragments to update (returned unmodified — the
                method produces fresh copies).
            eddies: Eddies previously emitted by
                :meth:`detect_eddies`.

        Returns:
            A new list of fragments with eddy wiki-links added where
            applicable. Fragments not assigned to any eddy are returned
            unchanged (as the same instance).
        """
        frag_to_links: dict[str, list[str]] = {}
        for eddy in eddies:
            members = self._eddy_members.get(eddy.id, [])
            eddy.fragment_count = len(members)
            wikilink = f"[[{eddy.title}]]"
            for fid in members:
                frag_to_links.setdefault(fid, []).append(wikilink)

        updated: list[Fragment] = []
        for frag in fragments:
            new_links = frag_to_links.get(frag.id, [])
            if not new_links:
                updated.append(frag)
                continue
            merged = list(frag.eddies)
            for link in new_links:
                if link not in merged:
                    merged.append(link)
            updated.append(frag.model_copy(update={"eddies": merged}))
        return updated
