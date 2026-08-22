"""Eddy detection — density-based clustering without temporal direction.

Provides :class:`EddyDetector`, which identifies *eddies* — topic
clusters that pool around a subject without a clear temporal narrative.
Unlike threads, which capture a theme evolving over time, an eddy is a
concern that returns to attention again and again across different
periods.

The algorithm mirrors the Creek pipeline's existing pure-numpy style
(no ``scikit-learn`` dependency):

0. Partition the corpus into independent clustering domains
   (:mod:`creek.link.segments`). DBSCAN assumes clusters separated by
   low-density regions; a continuous message stream has no such
   separator, so one epsilon-graph over seven years of chat is a single
   connected component whatever ``eps`` is chosen (issue #880). Message
   streams are therefore cut into conversation episodes *before* any
   similarity graph is built, and every other fragment shares one
   domain, leaving cross-source clustering exactly as it was.
1. Run DBSCAN over the fragment embedding vectors using cosine
   distance, per domain. Each dense cluster of at least ``min_samples``
   fragments is a candidate eddy. A cluster still larger than the
   configured ceiling is re-clustered at a tighter epsilon by
   :mod:`creek.link.cluster_limits`, and discarded to noise if even that
   fails — a guardrail, not the cure.
2. For each candidate, compute the Spearman rank correlation between
   chronological order and *content drift* (cosine distance from the
   oldest fragment). Low |correlation| (below
   :attr:`correlation_threshold`) indicates a scattered, non-directional
   topic — an eddy. High |correlation| indicates a thread-like monotonic
   drift and is filtered out.
3. Build an :class:`~creek.models.Eddy` per qualifying cluster with a
   title and description derived by :mod:`creek.link.naming`, a
   content-stable id derived from its membership, ``formed`` set to the
   median fragment creation date, and ``threads`` populated from any
   thread wiki-links already present on member fragments (i.e. threads
   that flow through the eddy).

The module also offers fragment-to-eddy assignment (adding wiki-links
to fragment ``eddies`` frontmatter).
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from creek.config import LinkingConfig
from creek.link import naming
from creek.link.cluster_limits import SplitPolicy, Tightening, split_oversized
from creek.link.neighbours import cosine_neighbours
from creek.link.segments import partition
from creek.models import Eddy
from creek.time import effective_authored_at, effective_authored_date

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import date

    from creek.link.segments import SegmentationConfig
    from creek.models import Fragment

logger = logging.getLogger(__name__)

_DEFAULT_EPS = 0.3
_DEFAULT_MIN_SAMPLES = 5
_DEFAULT_MIN_FRAGMENTS = 5
_DEFAULT_CORRELATION_THRESHOLD = 0.3
_DEFAULT_SPLIT_EPS_STEP = 0.05
_MAX_FALLBACK_TITLE_LEN = 50

_UNVISITED = -1
_NOISE = -2


@dataclass(frozen=True, slots=True)
class _NamingContext:
    """Corpus-wide facts every cluster in one run is measured against.

    Bundled into one object so the naming and guardrail parameters travel
    together through the detection call chain instead of as four parallel
    arguments.

    Attributes:
        document_frequency: How many corpus fragments carry each title
            content word, from :func:`creek.link.naming.document_frequency`.
        corpus_size: Number of embedded fragments the run considered.
        ceiling: Largest permitted cluster size for this corpus.
    """

    document_frequency: dict[str, int]
    corpus_size: int
    ceiling: int


def _stable_eddy_id(frags: list[Fragment]) -> str:
    """Return a deterministic ``eddy-<8hex>`` id for a cluster of *frags*.

    Mirrors :func:`creek.link.threads._stable_thread_id` (#718): eddy
    identity is derived from the sorted member fragment ids, so re-detecting
    the same cluster on the same corpus yields the same id — and therefore
    the same page filename, which lets :meth:`VaultWriter.write_eddy`
    recognise the existing page instead of minting a fresh one on every run.
    The model's random default (:func:`creek.models._generate_eddy_id`)
    remains for eddies constructed by hand, which have no membership to hash.

    Membership *changes* deliberately yield a new id: the cluster is a
    different eddy. That matters more now than before, because segmentation
    and splitting produce many smaller clusters (issue #880).

    Args:
        frags: The fragments composing the eddy (non-empty).

    Returns:
        A stable ``eddy-`` prefixed id, 8 hex chars of a SHA-256 digest.
    """
    member_key = ",".join(sorted(f.id for f in frags))
    digest = hashlib.sha256(member_key.encode("utf-8")).hexdigest()[:8]
    return f"eddy-{digest}"


def _apply_unique_titles(eddies: list[Eddy]) -> None:
    """Make every title in one detection run unique, in place.

    :meth:`EddyDetector.assign_fragments_to_eddies` writes ``[[<title>]]``
    onto each member fragment, so two eddies sharing a title would send both
    memberships to whichever page Obsidian resolved first.

    Args:
        eddies: The run's eddies, in emission order.
    """
    for eddy, title in zip(
        eddies,
        naming.disambiguate([e.title for e in eddies]),
        strict=True,
    ):
        eddy.title = title


def _cosine_distance(a: list[float], b: list[float]) -> float:
    """Return cosine *distance* (``1 - similarity``) clamped to ``[0, 2]``.

    Args:
        a: First vector.
        b: Second vector.

    Returns:
        Cosine distance; ``1.0`` when either vector has zero norm.
    """
    return 1.0 - naming.cosine(a, b)


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
    if 0.0 in (denom_t, denom_d):
        return 0.0
    return float(numerator / (denom_t * denom_d) ** 0.5)


def _median_date(fragments: list[Fragment]) -> date:
    """Return the median fragment authored date.

    Uses :func:`creek.time.effective_authored_at` /
    :func:`creek.time.effective_authored_date` (FEAT-031) so the median
    reflects when the fragments were *authored* — not the wall-clock
    moment they were ingested into the vault.

    Args:
        fragments: Non-empty list of fragments.

    Returns:
        The median authored date (lower-median for even counts).
    """
    sorted_frags = sorted(fragments, key=effective_authored_at)
    mid = len(sorted_frags) // 2
    return effective_authored_date(sorted_frags[mid])


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
        split_policy: Cluster-size guardrail applied to every detected
            cluster (issue #880).
        split_eps_step: How much ``eps`` tightens per re-clustering round.
        segmentation: Supplies the message-stream segmentation settings
            consumed by :func:`creek.link.segments.partition`.
    """

    def __init__(
        self,
        embeddings: dict[str, list[float]] | None = None,
        *,
        eps: float = _DEFAULT_EPS,
        min_samples: int = _DEFAULT_MIN_SAMPLES,
        correlation_threshold: float = _DEFAULT_CORRELATION_THRESHOLD,
        split_policy: SplitPolicy | None = None,
        split_eps_step: float = _DEFAULT_SPLIT_EPS_STEP,
        segmentation: SegmentationConfig | None = None,
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
            split_policy: Size ceiling and re-clustering budget. Defaults
                to :class:`~creek.link.cluster_limits.SplitPolicy`'s own
                defaults, whose absolute floor of 500 members keeps the
                guardrail inert on ordinary corpora.
            split_eps_step: Amount ``eps`` is tightened by on each
                re-clustering round. Defaults to ``0.05``.
            segmentation: Message-stream segmentation settings. Defaults
                to :class:`creek.config.LinkingConfig`'s own defaults, so
                a bare detector segments Discord and email exactly as the
                configured pipeline does.
        """
        self.embeddings: dict[str, list[float]] = embeddings or {}
        self.eps = eps
        self.min_samples = min_samples
        self.correlation_threshold = correlation_threshold
        self.split_policy = split_policy or SplitPolicy()
        self.split_eps_step = split_eps_step
        self.segmentation: SegmentationConfig = segmentation or LinkingConfig()
        self._eddy_members: dict[str, list[str]] = {}
        self._clusters_split = 0
        self._oversized_discarded = 0

    @classmethod
    def from_linking_config(
        cls,
        embeddings: dict[str, list[float]] | None,
        config: LinkingConfig,
    ) -> EddyDetector:
        """Build a detector wired to the vault's linking configuration.

        The single construction path into the detector, so a vault cannot
        cluster differently depending on the caller. ADR-0008 introduced
        this to reconcile two orchestrators; #1303 deleted the second one
        (``creek.link.linker``), leaving :mod:`creek.link.link_engine` as
        the only production caller — but the constraint still binds every
        future entry point.

        Args:
            embeddings: Mapping of fragment ID to embedding vector.
            config: The vault's ``linking`` configuration section.

        Returns:
            A detector using every configured threshold.
        """
        return cls(
            embeddings=embeddings,
            eps=config.eddy_eps,
            min_samples=config.eddy_min_samples,
            correlation_threshold=config.eddy_correlation_threshold,
            split_policy=SplitPolicy.from_linking_config(config),
            split_eps_step=config.eddy_split_eps_step,
            segmentation=config,
        )

    @property
    def eddy_members(self) -> dict[str, list[str]]:
        """Mapping of eddy ID to the fragment IDs that compose it.

        Populated by the most recent call to :meth:`detect_eddies`.

        Returns:
            A copy of the internal mapping. Empty until detection runs.
        """
        return self._eddy_members.copy()

    @property
    def clusters_split(self) -> int:
        """Clusters that exceeded the ceiling and were re-clustered.

        Returns:
            The count from the most recent :meth:`detect_eddies` call.
        """
        return self._clusters_split

    @property
    def oversized_discarded(self) -> int:
        """Fragments dropped to noise because their cluster would not split.

        Non-zero means those fragments carry no ``eddies:`` link at all —
        the deliberate cost of refusing to emit an unusable mega-page, and
        a number an operator should see.

        Returns:
            The count from the most recent :meth:`detect_eddies` call.
        """
        return self._oversized_discarded

    def detect_eddies(
        self,
        fragments: list[Fragment],
        min_fragments: int = _DEFAULT_MIN_FRAGMENTS,
    ) -> list[Eddy]:
        """Detect eddies across *fragments*.

        Partitions the corpus into clustering domains, runs DBSCAN within
        each, bounds any oversized cluster, then filters clusters that
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
            sorted by ``formed`` date ascending, with unique titles.
        """
        logger.info(
            "Detecting eddies among %d fragment(s) with eps=%.2f, min_samples=%d",
            len(fragments),
            self.eps,
            self.min_samples,
        )
        self._eddy_members = {}
        self._clusters_split = 0
        self._oversized_discarded = 0
        frag_by_id = {f.id: f for f in fragments if f.id in self.embeddings}
        if len(frag_by_id) < min_fragments:
            logger.info("Detected 0 eddies (insufficient embedded fragments)")
            return []

        corpus = list(frag_by_id.values())
        context = _NamingContext(
            document_frequency=naming.document_frequency(corpus),
            corpus_size=len(corpus),
            ceiling=self.split_policy.ceiling_for(len(corpus)),
        )
        eddies: list[Eddy] = []
        for domain in partition(corpus, config=self.segmentation):
            eddies.extend(self._detect_in_domain(domain, min_fragments, context))
        _apply_unique_titles(eddies)
        eddies.sort(key=lambda e: e.formed)
        logger.info(
            "Detected %d eddies (%d cluster(s) split, %d fragment(s) discarded)",
            len(eddies),
            self._clusters_split,
            self._oversized_discarded,
        )
        return eddies

    def _detect_in_domain(
        self,
        domain: list[Fragment],
        min_fragments: int,
        context: _NamingContext,
    ) -> list[Eddy]:
        """Detect the eddies of one clustering domain.

        Args:
            domain: Fragments of a single domain — one conversation
                episode, or the shared non-stream domain.
            min_fragments: Minimum cluster size required to emit an eddy.
            context: Corpus-wide naming and ceiling facts.

        Returns:
            The eddies found within *domain*.
        """
        frag_by_id = {f.id: f for f in domain}
        clusters = self._bounded_clusters(
            list(frag_by_id),
            min_fragments,
            context,
            naming.structural_label(domain),
        )
        return self._materialise_eddies(clusters, frag_by_id, context)

    def _bounded_clusters(
        self,
        ids: list[str],
        min_fragments: int,
        context: _NamingContext,
        domain: str,
    ) -> list[list[str]]:
        """Cluster *ids*, splitting anything above the configured ceiling.

        Splitting happens strictly *outside* :meth:`_dbscan`: it only ever
        re-partitions a membership list DBSCAN has already produced, and it
        passes the tightened epsilon as an argument rather than mutating
        :attr:`eps`. The #790 brute-force-equivalence contract, which pins
        raw ``_dbscan`` output, is therefore untouched.

        Args:
            ids: Fragment IDs of one domain (all have embeddings).
            min_fragments: Minimum cluster size worth keeping.
            context: Corpus-wide naming and ceiling facts.
            domain: Human-readable domain name, used in discard warnings.

        Returns:
            Cluster memberships, none larger than ``context.ceiling``.
        """
        bounded: list[list[str]] = []
        for members in self._dbscan(ids):
            if len(members) < min_fragments:
                continue
            if len(members) <= context.ceiling:
                bounded.append(members)
                continue
            self._clusters_split += 1
            result = split_oversized(
                members,
                ceiling=context.ceiling,
                policy=self.split_policy,
                recluster=self._recluster,
                tightening=Tightening.for_eddy_eps(self.eps, self.split_eps_step),
                domain=domain,
            )
            self._oversized_discarded += len(result.discarded)
            bounded.extend(sub for sub in result.accepted if len(sub) >= min_fragments)
        return bounded

    def _recluster(self, members: Sequence[str], eps: float) -> list[list[str]]:
        """Re-run DBSCAN over *members* at a tightened epsilon.

        Args:
            members: Membership of one oversized cluster.
            eps: Tightened cosine-distance radius for this round only.

        Returns:
            The subclusters found; noise points are simply absent.
        """
        return self._dbscan(list(members), eps=eps)

    def _dbscan(self, ids: list[str], *, eps: float | None = None) -> list[list[str]]:
        """Run DBSCAN over the provided fragment IDs.

        Uses cosine distance against :attr:`embeddings` with parameters
        :attr:`eps` and :attr:`min_samples`. Neighbour discovery is
        vectorised via :func:`creek.link.neighbours.cosine_neighbours`
        (issue #790): a fragment's neighbours are those with cosine
        similarity ``>= 1 - eps``, computed in memory-bounded blocks
        rather than an O(N²) pure-Python loop, yielding identical clusters.

        Args:
            ids: Fragment IDs to cluster (all must have embeddings).
            eps: Per-call radius override used by the splitter's
                re-clustering rounds. ``None`` (the default) uses
                :attr:`eps`. The override never mutates detector state, so
                concurrent rounds cannot leak parameters into each other.

        Returns:
            A list of clusters, each a list of fragment IDs. Noise
            points are excluded.
        """
        radius = self.eps if eps is None else eps
        n = len(ids)
        labels = [_UNVISITED] * n
        # Vectorised neighbour discovery (issue #790): a fragment's neighbours
        # are those with cosine similarity >= 1 - eps (i.e. cosine distance
        # <= eps), computed in memory-bounded blocks rather than an O(N²)
        # pure-Python loop. Clusters are identical to the exhaustive path.
        vectors = [self.embeddings[fid] for fid in ids]
        neighbour_cache = cosine_neighbours(vectors, 1.0 - radius)
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
        queue = neighbour_cache[seed].copy()
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
        context: _NamingContext,
    ) -> list[Eddy]:
        """Convert bounded clusters into :class:`Eddy` objects.

        Filters out clusters with thread-like temporal progression
        (|Spearman| at or above :attr:`correlation_threshold`). The
        minimum-size floor has already been applied by
        :meth:`_bounded_clusters`. Side effect: adds to
        :attr:`_eddy_members`.

        Args:
            clusters: Cluster memberships as lists of IDs.
            frag_by_id: Fragment lookup table for this domain.
            context: Corpus-wide naming facts.

        Returns:
            Constructed eddies for every qualifying cluster.
        """
        eddies: list[Eddy] = []
        for members in clusters:
            cluster_frags = sorted(
                (frag_by_id[fid] for fid in members),
                key=effective_authored_at,
            )
            if self._has_temporal_direction(cluster_frags):
                continue
            eddy = self._build_eddy(cluster_frags, context)
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

    @staticmethod
    def _build_eddy(frags: list[Fragment], context: _NamingContext) -> Eddy:
        """Construct an :class:`Eddy` from its constituent fragments.

        Args:
            frags: Fragments belonging to the cluster, chronologically
                sorted (non-empty).
            context: Corpus-wide naming facts.

        Returns:
            A populated :class:`~creek.models.Eddy`, including a
            never-empty ``description`` — historically omitted here, which
            is why generated pages shipped ``description: ''``.
        """
        return Eddy(
            id=_stable_eddy_id(frags),
            title=EddyDetector._generate_title(frags, context),
            formed=_median_date(frags),
            fragment_count=len(frags),
            threads=EddyDetector._flowing_threads(frags),
            description=naming.cluster_description(
                frags,
                context.document_frequency,
                context.corpus_size,
            ),
        )

    @staticmethod
    def _generate_title(frags: list[Fragment], context: _NamingContext) -> str:
        """Generate a title for the cluster.

        Delegates to :func:`creek.link.naming.cluster_title`, which applies
        the inverse document frequency this method's docstring used to
        claim and never implemented: a term carried by most of the corpus
        — ``messages``, on a Discord-heavy vault — can no longer become a
        topic label, and a cluster left with no surviving term is named
        from its structure (dominant channel plus date span) instead.

        A cluster whose member titles contain *no content words at all*
        (pure punctuation, single letters) is a different case: there is
        nothing for IDF to suppress, so the historical fallback to the
        earliest member's own title is kept.

        Args:
            frags: Fragments in the cluster (non-empty).
            context: Corpus-wide naming facts.

        Returns:
            A short, non-empty title string.
        """
        if not any(naming.content_words(f.title) for f in frags):
            return frags[0].title[:_MAX_FALLBACK_TITLE_LEN]
        return naming.cluster_title(
            frags,
            context.document_frequency,
            context.corpus_size,
        )

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
            merged = frag.eddies.copy()
            for link in new_links:
                if link not in merged:
                    merged.append(link)
            updated.append(frag.model_copy(update={"eddies": merged}))
        return updated
