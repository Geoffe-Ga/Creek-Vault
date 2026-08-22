"""Thread detection — sliding time window plus topic consistency.

Provides the :class:`ThreadDetector` which identifies recurring narrative
threads across fragments. The algorithm sorts fragments chronologically,
walks a sliding time window, and unions fragments that are *topic
consistent* (semantic similarity above a threshold AND frequency
agreement) into clusters. Clusters that meet the configured minimum
fragment count become :class:`~creek.models.Thread` instances.

Time-bucketing routes through :func:`creek.time.effective_authored_at`
and :func:`creek.time.effective_authored_date` (FEAT-031) so a multi-
year corpus ingested in one batch produces honest first/last bounds
and window comparisons — instead of collapsing every fragment to a
single ingest-time wall-clock.

The module also offers fragment-to-thread assignment (adding wiki-links
to fragment ``threads`` frontmatter) and a heuristic for suggesting
thread merges when two threads appear to cover the same topic.
"""

from __future__ import annotations

import hashlib
import logging
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING

from tqdm import tqdm

from creek.config import LinkingConfig
from creek.link import naming
from creek.link.cluster_limits import SplitPolicy, Tightening, split_oversized
from creek.link.segments import partition
from creek.models import Frequency, Thread, ThreadStatus
from creek.time import effective_authored_at, effective_authored_date, now_la

if TYPE_CHECKING:
    from collections.abc import Sequence

    from creek.link.segments import SegmentationConfig
    from creek.models import Fragment

logger = logging.getLogger(__name__)

_DEFAULT_WINDOW_DAYS = 30
_DEFAULT_SIMILARITY_THRESHOLD = 0.6
_DEFAULT_MIN_FRAGMENTS = 3
_DEFAULT_MERGE_JACCARD = 0.3
_ACTIVE_DAYS = 30
_DORMANT_DAYS = 180
_DEFAULT_SPLIT_SIMILARITY_STEP = 0.1
_MAX_FALLBACK_TITLE_LEN = 50
#: Below this many fragments a domain's pairing loop finishes instantly, so a
#: progress bar for it is noise. Segmentation makes small domains the norm.
_PROGRESS_MIN_FRAGMENTS = 500


@dataclass(frozen=True, slots=True)
class _NamingContext:
    """Corpus-wide facts every cluster in one run is measured against.

    Attributes:
        document_frequency: How many corpus fragments carry each title
            content word, from :func:`creek.link.naming.document_frequency`.
        corpus_size: Number of fragments the run considered.
        ceiling: Largest permitted cluster size for this corpus.
    """

    document_frequency: dict[str, int]
    corpus_size: int
    ceiling: int


def _apply_unique_titles(threads: list[Thread]) -> None:
    """Make every title in one detection run unique, in place.

    :meth:`ThreadDetector.assign_fragments_to_threads` writes
    ``[[<title>]]`` onto each member fragment, so two threads sharing a
    title would send both memberships to whichever page resolved first.

    Args:
        threads: The run's threads, in emission order.
    """
    for thread, title in zip(
        threads,
        naming.disambiguate([t.title for t in threads]),
        strict=True,
    ):
        thread.title = title


class _UnionFind:
    """Disjoint-set structure used to merge clusters across windows.

    Stores parent pointers keyed by fragment ID. Path compression keeps
    ``find`` near-constant amortised time.
    """

    def __init__(self) -> None:
        """Initialise with no members."""
        self._parent: dict[str, str] = {}

    def add(self, item: str) -> None:
        """Add *item* as its own root if not already present.

        Args:
            item: The element to ensure exists in the structure.
        """
        if item not in self._parent:
            self._parent[item] = item

    def find(self, item: str) -> str:
        """Return the representative root for *item*.

        Args:
            item: The element to look up. Added as a singleton if absent.

        Returns:
            The root ID of *item*'s set.
        """
        self.add(item)
        root = item
        while self._parent[root] != root:
            root = self._parent[root]
        # Path compression
        cur = item
        while self._parent[cur] != root:
            nxt = self._parent[cur]
            self._parent[cur] = root
            cur = nxt
        return root

    def union(self, a: str, b: str) -> None:
        """Union the sets containing *a* and *b*.

        Args:
            a: First element.
            b: Second element.
        """
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self._parent[rb] = ra

    def groups(self) -> dict[str, list[str]]:
        """Return current groups keyed by their root representative.

        Returns:
            Mapping of root ID to the list of member IDs.
        """
        out: dict[str, list[str]] = {}
        for member in list(self._parent.keys()):
            root = self.find(member)
            out.setdefault(root, []).append(member)
        return out


def _stable_thread_id(frags: list[Fragment]) -> str:
    """Return a deterministic ``thread-<8hex>`` id for a cluster of *frags*.

    Thread identity is derived from the sorted member fragment ids so that
    re-detecting the same cluster on the same corpus yields the same id — and
    therefore the same page filename, which lets :meth:`VaultWriter.write_thread`
    recognise the existing page and makes bulk page materialisation idempotent
    (#718). A random uuid (the historical default) produced a fresh page on every
    run. Membership *changes* deliberately yield a new id: the cluster is a
    different thread, and the stale page is a documented re-detection edge.

    Args:
        frags: The fragments composing the thread (non-empty).

    Returns:
        A stable ``thread-`` prefixed id, 8 hex chars of a SHA-256 digest.
    """
    member_key = ",".join(sorted(f.id for f in frags))
    digest = hashlib.sha256(member_key.encode("utf-8")).hexdigest()[:8]
    return f"thread-{digest}"


class ThreadDetector:
    """Detect narrative threads via sliding time window plus topic consistency.

    Two fragments are considered *topic consistent* when their primary
    or secondary frequencies overlap **and** (when embeddings are
    available) their cosine similarity exceeds
    :attr:`similarity_threshold`. Threads are clusters of at least
    ``min_fragments`` fragments connected (transitively) under that
    relation within the configured sliding time window.

    Attributes:
        embeddings: Optional fragment-ID to embedding-vector mapping.
            When omitted, topic consistency falls back to frequency
            agreement alone.
        window_days: Width of the sliding time window in days.
        similarity_threshold: Minimum cosine similarity to count as a
            semantic match.
        merge_jaccard: Title-token Jaccard similarity above which two
            threads are suggested for merging.
        union_without_embeddings: Whether a pair missing an embedding may
            union on frequency overlap alone on an otherwise-embedded
            corpus (issue #880 — closed by default).
        split_policy: Cluster-size guardrail applied to every component.
        split_similarity_step: How much the similarity threshold tightens
            per re-clustering round.
        segmentation: Supplies the message-stream segmentation settings
            consumed by :func:`creek.link.segments.partition`.
    """

    def __init__(
        self,
        embeddings: dict[str, list[float]] | None = None,
        *,
        window_days: int = _DEFAULT_WINDOW_DAYS,
        similarity_threshold: float = _DEFAULT_SIMILARITY_THRESHOLD,
        merge_jaccard: float = _DEFAULT_MERGE_JACCARD,
        now: datetime | None = None,
        union_without_embeddings: bool = False,
        split_policy: SplitPolicy | None = None,
        split_similarity_step: float = _DEFAULT_SPLIT_SIMILARITY_STEP,
        segmentation: SegmentationConfig | None = None,
    ) -> None:
        """Initialise the detector.

        Args:
            embeddings: Optional mapping of fragment ID to embedding
                vector (e.g. produced by
                :class:`creek.link.embeddings.EmbeddingLinker`).
            window_days: Sliding window width in days. Defaults to 30.
            similarity_threshold: Cosine similarity threshold for the
                semantic component of topic consistency. Defaults to 0.6.
            merge_jaccard: Title-token Jaccard threshold used by
                :meth:`suggest_merges`. Defaults to 0.3.
            now: Reference "now" for status calculation; defaults to
                :func:`creek.time.now_la` (America/Los_Angeles tz-aware).
                Useful for deterministic tests.
            union_without_embeddings: When ``True``, a pair whose
                fragments lack embeddings unions on frequency overlap
                alone even though the corpus is otherwise embedded.
                Defaults to ``False``. A detector given *no* embeddings at
                all still runs in frequency-only mode regardless — that is
                the documented no-embeddings fallback, not the fail-open.
            split_policy: Size ceiling and re-clustering budget. Defaults
                to :class:`~creek.link.cluster_limits.SplitPolicy`'s own
                defaults, whose absolute floor keeps it inert on ordinary
                corpora.
            split_similarity_step: Amount the similarity threshold is
                raised on each re-clustering round. Defaults to ``0.1``.
            segmentation: Message-stream segmentation settings. Defaults
                to :class:`creek.config.LinkingConfig`'s own defaults.
        """
        self.embeddings: dict[str, list[float]] = embeddings or {}
        self.window_days = window_days
        self.similarity_threshold = similarity_threshold
        self.merge_jaccard = merge_jaccard
        self.union_without_embeddings = union_without_embeddings
        self.split_policy = split_policy or SplitPolicy()
        self.split_similarity_step = split_similarity_step
        self.segmentation: SegmentationConfig = segmentation or LinkingConfig()
        self._now = now or now_la()
        self._thread_members: dict[str, list[str]] = {}
        self._clusters_split = 0
        self._oversized_discarded = 0

    @classmethod
    def from_linking_config(
        cls,
        embeddings: dict[str, list[float]] | None,
        config: LinkingConfig,
        *,
        now: datetime | None = None,
    ) -> ThreadDetector:
        """Build a detector wired to the vault's linking configuration.

        The single construction path into the detector, so a vault cannot
        cluster differently depending on the caller. ADR-0008 introduced
        this to reconcile two orchestrators; #1303 deleted the second one
        (``creek.link.linker``), leaving :mod:`creek.link.link_engine` as
        the only production caller — but the constraint still binds every
        future entry point.

        Args:
            embeddings: Optional mapping of fragment ID to embedding vector.
            config: The vault's ``linking`` configuration section.
            now: Reference "now" for status calculation.

        Returns:
            A detector using every configured threshold.
        """
        return cls(
            embeddings=embeddings,
            window_days=config.thread_window_days,
            similarity_threshold=config.thread_similarity_threshold,
            now=now,
            union_without_embeddings=config.thread_union_without_embeddings,
            split_policy=SplitPolicy.from_linking_config(config),
            split_similarity_step=config.thread_split_similarity_step,
            segmentation=config,
        )

    @property
    def thread_members(self) -> dict[str, list[str]]:
        """Mapping of thread ID to the fragment IDs that compose it.

        Populated by the most recent call to :meth:`detect_threads`.

        Returns:
            A copy of the internal mapping. Empty until detection runs.
        """
        return self._thread_members.copy()

    @property
    def clusters_split(self) -> int:
        """Components that exceeded the ceiling and were re-clustered.

        Returns:
            The count from the most recent :meth:`detect_threads` call.
        """
        return self._clusters_split

    @property
    def oversized_discarded(self) -> int:
        """Fragments dropped to noise because their component would not split.

        Non-zero means those fragments carry no ``threads:`` link at all.

        Returns:
            The count from the most recent :meth:`detect_threads` call.
        """
        return self._oversized_discarded

    def detect_threads(
        self,
        fragments: list[Fragment],
        min_fragments: int = _DEFAULT_MIN_FRAGMENTS,
    ) -> list[Thread]:
        """Detect narrative threads in *fragments*.

        Partitions the corpus into clustering domains, and within each
        sorts fragments chronologically and unions any pair inside the
        configured sliding window that is topic consistent. Connected
        components with at least *min_fragments* members become
        :class:`~creek.models.Thread` instances.

        Partitioning is what stops seven years of continuous chat chaining
        into one component (issue #880): the 30-day window bounds *pairing*
        only, while :meth:`_UnionFind.union` is unbounded, so overlapping
        windows previously took the transitive closure across the entire
        corpus. Union-find is disjoint per domain, so merging the per-domain
        results is a plain concatenation.

        Args:
            fragments: Fragments to scan for thread patterns.
            min_fragments: Minimum cluster size required to emit a
                thread. Defaults to 3.

        Returns:
            A list of detected :class:`~creek.models.Thread` objects,
            sorted by ``first_seen`` ascending, with unique titles.
        """
        logger.info(
            "Detecting threads among %d fragment(s) with %d-day window",
            len(fragments),
            self.window_days,
        )

        self._thread_members = {}
        self._clusters_split = 0
        self._oversized_discarded = 0
        if len(fragments) < min_fragments:
            return []

        sorted_frags = sorted(fragments, key=effective_authored_at)
        context = _NamingContext(
            document_frequency=naming.document_frequency(sorted_frags),
            corpus_size=len(sorted_frags),
            ceiling=self.split_policy.ceiling_for(len(sorted_frags)),
        )
        threads: list[Thread] = []
        for domain in partition(sorted_frags, config=self.segmentation):
            threads.extend(self._detect_in_domain(domain, min_fragments, context))

        _apply_unique_titles(threads)
        threads.sort(key=lambda t: t.first_seen)
        logger.info(
            "Detected %d thread(s) (%d component(s) split, %d fragment(s) discarded)",
            len(threads),
            self._clusters_split,
            self._oversized_discarded,
        )
        return threads

    def _detect_in_domain(
        self,
        domain: list[Fragment],
        min_fragments: int,
        context: _NamingContext,
    ) -> list[Thread]:
        """Detect the threads of one clustering domain.

        Args:
            domain: Fragments of a single domain — one conversation
                episode, or the shared non-stream domain.
            min_fragments: Minimum cluster size required to emit a thread.
            context: Corpus-wide naming and ceiling facts.

        Returns:
            The threads found within *domain*.
        """
        ordered = sorted(domain, key=effective_authored_at)
        frag_by_id = {f.id: f for f in ordered}
        components = self._bounded_components(
            self._cluster(ordered),
            frag_by_id,
            min_fragments=min_fragments,
            ceiling=context.ceiling,
            domain=naming.structural_label(ordered),
        )
        return self._materialise_threads(components, frag_by_id, context)

    def _cluster(
        self,
        sorted_frags: list[Fragment],
        *,
        similarity_threshold: float | None = None,
    ) -> _UnionFind:
        """Walk the sliding window, unioning topic-consistent pairs.

        Args:
            sorted_frags: Fragments pre-sorted by
                :func:`creek.time.effective_authored_at` ascending
                (FEAT-031). Window comparisons use the same helper so
                a multi-year corpus ingested in one batch does not
                collapse into a single window.
            similarity_threshold: Per-call override used by the splitter's
                re-clustering rounds. ``None`` (the default) uses
                :attr:`similarity_threshold`. The override never mutates
                detector state.

        Returns:
            A populated :class:`_UnionFind` covering every fragment ID.
        """
        threshold = (
            self.similarity_threshold
            if similarity_threshold is None
            else similarity_threshold
        )
        uf = _UnionFind()
        for frag in sorted_frags:
            uf.add(frag.id)

        window = timedelta(days=self.window_days)
        # OPS-004: outer loop drives the wall-time on a 10k-vault link
        # rebuild. tqdm in TTYs, silent elsewhere — and silent for the small
        # domains segmentation now produces, which finish instantly.
        outer = tqdm(
            enumerate(sorted_frags),
            total=len(sorted_frags),
            desc="Threads",
            unit="frag",
            disable=not sys.stderr.isatty()
            or len(sorted_frags) < _PROGRESS_MIN_FRAGMENTS,
        )
        for i, frag_a in outer:
            anchor_at = effective_authored_at(frag_a)
            for frag_b in sorted_frags[i + 1 :]:
                if effective_authored_at(frag_b) - anchor_at > window:
                    break
                if self._topic_consistent(frag_a, frag_b, threshold):
                    uf.union(frag_a.id, frag_b.id)
        return uf

    def _bounded_components(
        self,
        uf: _UnionFind,
        frag_by_id: dict[str, Fragment],
        *,
        min_fragments: int,
        ceiling: int,
        domain: str,
    ) -> list[list[str]]:
        """Return the domain's components, splitting any above the ceiling.

        Splitting happens strictly outside :meth:`_cluster`: it only ever
        re-partitions a membership list already produced, and it passes the
        tightened threshold as an argument rather than mutating
        :attr:`similarity_threshold`.

        Args:
            uf: Union-find produced by :meth:`_cluster` for this domain.
            frag_by_id: Fragment lookup table for this domain.
            min_fragments: Minimum component size worth keeping.
            ceiling: Largest permitted component size for this corpus.
            domain: Human-readable domain name, used in discard warnings.

        Returns:
            Component memberships, none larger than *ceiling*.
        """

        def recluster(members: Sequence[str], threshold: float) -> list[list[str]]:
            """Re-cluster *members* at a tightened similarity threshold.

            Args:
                members: Membership of one oversized component.
                threshold: Tightened cosine floor for this round only.

            Returns:
                The subcomponents found, including singletons.
            """
            subset = sorted(
                (frag_by_id[fid] for fid in members),
                key=effective_authored_at,
            )
            groups = self._cluster(subset, similarity_threshold=threshold)
            return list(groups.groups().values())

        bounded: list[list[str]] = []
        for member_ids in uf.groups().values():
            if len(member_ids) < min_fragments:
                continue
            if len(member_ids) <= ceiling:
                bounded.append(member_ids)
                continue
            self._clusters_split += 1
            result = split_oversized(
                member_ids,
                ceiling=ceiling,
                policy=self.split_policy,
                recluster=recluster,
                tightening=Tightening.for_thread_similarity(
                    self.similarity_threshold,
                    self.split_similarity_step,
                ),
                domain=domain,
            )
            self._oversized_discarded += len(result.discarded)
            bounded.extend(sub for sub in result.accepted if len(sub) >= min_fragments)
        return bounded

    def _materialise_threads(
        self,
        components: list[list[str]],
        frag_by_id: dict[str, Fragment],
        context: _NamingContext,
    ) -> list[Thread]:
        """Convert bounded components into :class:`Thread` objects.

        The minimum-size floor has already been applied by
        :meth:`_bounded_components`.

        Args:
            components: Component memberships as lists of IDs.
            frag_by_id: Fragment lookup table for this domain.
            context: Corpus-wide naming facts.

        Returns:
            The list of threads built from qualifying components. Side
            effect: adds to :attr:`_thread_members`.
        """
        threads: list[Thread] = []
        for member_ids in components:
            cluster = sorted(
                (frag_by_id[fid] for fid in member_ids),
                key=effective_authored_at,
            )
            thread = self._build_thread(cluster, context)
            threads.append(thread)
            self._thread_members[thread.id] = [f.id for f in cluster]
        return threads

    def _topic_consistent(
        self,
        a: Fragment,
        b: Fragment,
        similarity_threshold: float,
    ) -> bool:
        """Return whether *a* and *b* should be grouped into one thread.

        The missing-embedding case is explicit rather than permissive
        (issue #880). A detector holding no embeddings at all is in the
        documented frequency-only mode and still unions. A detector that
        *does* hold embeddings but is missing one for this pair — a
        partially-embedded vault — refuses to union unless
        :attr:`union_without_embeddings` says otherwise, because the old
        fail-open chained far more aggressively than the similarity gate
        it was standing in for.

        Args:
            a: First fragment.
            b: Second fragment.
            similarity_threshold: Cosine floor a pair must strictly exceed.

        Returns:
            ``True`` if the pair belongs in one thread.
        """
        if not self._frequency_overlap(a, b):
            return False
        emb_a = self.embeddings.get(a.id)
        emb_b = self.embeddings.get(b.id)
        if emb_a is None or emb_b is None:
            return not self.embeddings or self.union_without_embeddings
        return naming.cosine(emb_a, emb_b) > similarity_threshold

    @staticmethod
    def _frequency_overlap(a: Fragment, b: Fragment) -> bool:
        """Return whether *a* and *b* share a primary or secondary frequency.

        ``UNCLASSIFIED`` is treated as no signal.

        Args:
            a: First fragment.
            b: Second fragment.

        Returns:
            ``True`` if any classified frequency appears in both.
        """
        unclassified = Frequency.UNCLASSIFIED.value
        primary_a = str(a.frequency.primary)
        primary_b = str(b.frequency.primary)
        if primary_a == primary_b and primary_a != unclassified:
            return True
        sec_a: set[str] = {str(f) for f in a.frequency.secondary}
        sec_a.add(primary_a)
        sec_a.discard(unclassified)
        sec_b: set[str] = {str(f) for f in b.frequency.secondary}
        sec_b.add(primary_b)
        sec_b.discard(unclassified)
        return bool(sec_a & sec_b)

    def _build_thread(self, frags: list[Fragment], context: _NamingContext) -> Thread:
        """Construct a :class:`Thread` from its constituent fragments.

        Args:
            frags: Fragments belonging to the cluster (non-empty).
            context: Corpus-wide naming facts.

        Returns:
            A populated :class:`~creek.models.Thread`, including a
            never-empty ``description`` — historically omitted here, which
            is why generated pages shipped ``description: ''``.
        """
        # FEAT-031: bucket on the authored-date precedence so a multi-
        # year corpus ingested in one batch surfaces honest first/last
        # bounds instead of collapsing to today's wall-clock.
        first_seen = min(effective_authored_date(f) for f in frags)
        last_seen = max(effective_authored_date(f) for f in frags)
        return Thread(
            id=_stable_thread_id(frags),
            title=self._generate_title(frags, context),
            status=self._compute_status(last_seen),
            first_seen=first_seen,
            last_seen=last_seen,
            frequency_affinity=self._frequency_affinity(frags),
            fragment_count=len(frags),
            description=naming.cluster_description(
                frags,
                context.document_frequency,
                context.corpus_size,
            ),
        )

    def _compute_status(self, last_seen: date) -> ThreadStatus:
        """Compute lifecycle status from how long ago *last_seen* was.

        Args:
            last_seen: Date of the most recent fragment in the thread.

        Returns:
            ``ACTIVE`` if within ``_ACTIVE_DAYS``, ``DORMANT`` if within
            ``_DORMANT_DAYS``, otherwise ``RESOLVED``.
        """
        days_since = (self._now.date() - last_seen).days
        if days_since <= _ACTIVE_DAYS:
            return ThreadStatus.ACTIVE
        if days_since <= _DORMANT_DAYS:
            return ThreadStatus.DORMANT
        return ThreadStatus.RESOLVED

    @staticmethod
    def _generate_title(frags: list[Fragment], context: _NamingContext) -> str:
        """Generate a human-readable title for the cluster.

        Delegates to :func:`creek.link.naming.cluster_title`, so a term
        carried by most of the corpus cannot become a topic label and a
        cluster left with no surviving term is named from its structure
        (dominant channel plus date span) instead.

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
    def _frequency_affinity(frags: list[Fragment]) -> list[Frequency]:
        """Return the most common primary frequencies in the cluster.

        Args:
            frags: Fragments in the cluster.

        Returns:
            All frequencies tied for the highest primary-frequency count
            across the cluster (excluding ``UNCLASSIFIED``).
        """
        counts: Counter[str] = Counter()
        unclassified = Frequency.UNCLASSIFIED.value
        for frag in frags:
            primary = frag.frequency.primary
            if primary != unclassified:
                counts[primary] += 1
        if not counts:
            return []
        max_count = max(counts.values())
        return [
            Frequency(value) for value, count in counts.items() if count == max_count
        ]

    def assign_fragments_to_threads(
        self,
        fragments: list[Fragment],
        threads: list[Thread],
    ) -> list[Fragment]:
        """Add thread wiki-links to each fragment and refresh thread counts.

        Uses the membership map captured by the most recent
        :meth:`detect_threads` call to decide which threads each fragment
        belongs to. A wiki-link of the form ``[[<thread title>]]`` is
        appended to each fragment's ``threads`` field, with duplicates
        skipped. Each thread's ``fragment_count`` is updated in place to
        reflect its current membership.

        Args:
            fragments: Fragments to update (returned unmodified — the
                method produces fresh copies).
            threads: Threads previously emitted by
                :meth:`detect_threads`.

        Returns:
            A new list of fragments with thread wiki-links added where
            applicable. Fragments not assigned to any thread are
            returned unchanged (as the same instance).
        """
        frag_to_links: dict[str, list[str]] = {}
        for thread in threads:
            members = self._thread_members.get(thread.id, [])
            thread.fragment_count = len(members)
            wikilink = f"[[{thread.title}]]"
            for fid in members:
                frag_to_links.setdefault(fid, []).append(wikilink)

        updated: list[Fragment] = []
        for frag in fragments:
            new_links = frag_to_links.get(frag.id, [])
            if not new_links:
                updated.append(frag)
                continue
            merged = frag.threads.copy()
            for link in new_links:
                if link not in merged:
                    merged.append(link)
            updated.append(frag.model_copy(update={"threads": merged}))
        return updated

    def suggest_merges(
        self,
        threads: list[Thread],
    ) -> list[tuple[Thread, Thread]]:
        """Suggest pairs of threads that appear to cover the same topic.

        Two threads are flagged when their title content-word Jaccard
        similarity meets or exceeds :attr:`merge_jaccard` and they share
        at least one frequency in their ``frequency_affinity``.

        Args:
            threads: Threads to compare pairwise.

        Returns:
            A list of ``(thread_a, thread_b)`` tuples, with ``a`` ordered
            before ``b`` by their position in *threads*.
        """
        suggestions: list[tuple[Thread, Thread]] = []
        for i, thread_a in enumerate(threads):
            for thread_b in threads[i + 1 :]:
                if self._should_merge(thread_a, thread_b):
                    suggestions.append((thread_a, thread_b))
        return suggestions

    def _should_merge(self, a: Thread, b: Thread) -> bool:
        """Return whether two threads should be suggested for merging.

        Args:
            a: First thread.
            b: Second thread.

        Returns:
            ``True`` if title-token Jaccard similarity passes the
            threshold and frequency affinities intersect.
        """
        words_a = set(naming.content_words(a.title))
        words_b = set(naming.content_words(b.title))
        if not words_a or not words_b:
            return False
        union = words_a | words_b
        jaccard = len(words_a & words_b) / len(union)
        if jaccard < self.merge_jaccard:
            return False
        return bool(set(a.frequency_affinity) & set(b.frequency_affinity))
