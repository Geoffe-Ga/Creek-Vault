"""Thread detection — sliding time window plus topic consistency.

Provides the :class:`ThreadDetector` which identifies recurring narrative
threads across fragments. The algorithm sorts fragments chronologically,
walks a sliding time window, and unions fragments that are *topic
consistent* (semantic similarity above a threshold AND frequency
agreement) into clusters. Clusters that meet the configured minimum
fragment count become :class:`~creek.models.Thread` instances.

The module also offers fragment-to-thread assignment (adding wiki-links
to fragment ``threads`` frontmatter) and a heuristic for suggesting
thread merges when two threads appear to cover the same topic.
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING

import numpy as np

from creek.models import Frequency, Thread, ThreadStatus

if TYPE_CHECKING:
    from creek.models import Fragment

logger = logging.getLogger(__name__)

_DEFAULT_WINDOW_DAYS = 30
_DEFAULT_SIMILARITY_THRESHOLD = 0.6
_DEFAULT_MIN_FRAGMENTS = 3
_DEFAULT_MERGE_JACCARD = 0.3
_ACTIVE_DAYS = 30
_DORMANT_DAYS = 180
_TITLE_TOP_WORDS = 3
_MIN_TITLE_WORD_LEN = 3

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


def _cosine(a: list[float], b: list[float]) -> float:
    """Compute cosine similarity between two equal-length vectors.

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
    """

    def __init__(
        self,
        embeddings: dict[str, list[float]] | None = None,
        *,
        window_days: int = _DEFAULT_WINDOW_DAYS,
        similarity_threshold: float = _DEFAULT_SIMILARITY_THRESHOLD,
        merge_jaccard: float = _DEFAULT_MERGE_JACCARD,
        now: datetime | None = None,
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
                :func:`datetime.now`. Useful for deterministic tests.
        """
        self.embeddings: dict[str, list[float]] = embeddings or {}
        self.window_days = window_days
        self.similarity_threshold = similarity_threshold
        self.merge_jaccard = merge_jaccard
        self._now = now or datetime.now()
        self._thread_members: dict[str, list[str]] = {}

    @property
    def thread_members(self) -> dict[str, list[str]]:
        """Mapping of thread ID to the fragment IDs that compose it.

        Populated by the most recent call to :meth:`detect_threads`.

        Returns:
            A copy of the internal mapping. Empty until detection runs.
        """
        return dict(self._thread_members)

    def detect_threads(
        self,
        fragments: list[Fragment],
        min_fragments: int = _DEFAULT_MIN_FRAGMENTS,
    ) -> list[Thread]:
        """Detect narrative threads in *fragments*.

        Sorts fragments chronologically, then unions any pair within the
        configured sliding window that is topic consistent. Connected
        components with at least *min_fragments* members become
        :class:`~creek.models.Thread` instances.

        Args:
            fragments: Fragments to scan for thread patterns.
            min_fragments: Minimum cluster size required to emit a
                thread. Defaults to 3.

        Returns:
            A list of detected :class:`~creek.models.Thread` objects,
            sorted by ``first_seen`` ascending.
        """
        logger.info(
            "Detecting threads among %d fragment(s) with %d-day window",
            len(fragments),
            self.window_days,
        )

        if len(fragments) < min_fragments:
            self._thread_members = {}
            return []

        sorted_frags = sorted(fragments, key=lambda f: f.created)
        frag_by_id = {f.id: f for f in sorted_frags}
        uf = self._cluster(sorted_frags)
        threads = self._materialise_threads(uf, frag_by_id, min_fragments)

        threads.sort(key=lambda t: t.first_seen)
        logger.info("Detected %d thread(s)", len(threads))
        return threads

    def _cluster(self, sorted_frags: list[Fragment]) -> _UnionFind:
        """Walk the sliding window, unioning topic-consistent pairs.

        Args:
            sorted_frags: Fragments pre-sorted by ``created`` ascending.

        Returns:
            A populated :class:`_UnionFind` covering every fragment ID.
        """
        uf = _UnionFind()
        for frag in sorted_frags:
            uf.add(frag.id)

        window = timedelta(days=self.window_days)
        for i, frag_a in enumerate(sorted_frags):
            for frag_b in sorted_frags[i + 1 :]:
                if frag_b.created - frag_a.created > window:
                    break
                if self._topic_consistent(frag_a, frag_b):
                    uf.union(frag_a.id, frag_b.id)
        return uf

    def _materialise_threads(
        self,
        uf: _UnionFind,
        frag_by_id: dict[str, Fragment],
        min_fragments: int,
    ) -> list[Thread]:
        """Convert union-find groups into :class:`Thread` objects.

        Args:
            uf: Union-find with one node per fragment.
            frag_by_id: Fragment lookup table.
            min_fragments: Minimum cluster size to keep.

        Returns:
            The list of threads built from qualifying clusters. Side
            effect: populates :attr:`_thread_members`.
        """
        self._thread_members = {}
        threads: list[Thread] = []
        for member_ids in uf.groups().values():
            if len(member_ids) < min_fragments:
                continue
            cluster = sorted(
                (frag_by_id[fid] for fid in member_ids),
                key=lambda f: f.created,
            )
            thread = self._build_thread(cluster)
            threads.append(thread)
            self._thread_members[thread.id] = [f.id for f in cluster]
        return threads

    def _topic_consistent(self, a: Fragment, b: Fragment) -> bool:
        """Return whether *a* and *b* should be grouped into one thread.

        Args:
            a: First fragment.
            b: Second fragment.

        Returns:
            ``True`` if frequencies overlap and (when embeddings are
            present) cosine similarity exceeds the configured threshold.
        """
        if not self._frequency_overlap(a, b):
            return False
        emb_a = self.embeddings.get(a.id)
        emb_b = self.embeddings.get(b.id)
        if emb_a is None or emb_b is None:
            return True
        return _cosine(emb_a, emb_b) > self.similarity_threshold

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

    def _build_thread(self, frags: list[Fragment]) -> Thread:
        """Construct a :class:`Thread` from its constituent fragments.

        Args:
            frags: Fragments belonging to the cluster (non-empty).

        Returns:
            A populated :class:`~creek.models.Thread`.
        """
        first_seen = min(f.created for f in frags).date()
        last_seen = max(f.created for f in frags).date()
        return Thread(
            title=self._generate_title(frags),
            status=self._compute_status(last_seen),
            first_seen=first_seen,
            last_seen=last_seen,
            frequency_affinity=self._frequency_affinity(frags),
            fragment_count=len(frags),
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
    def _generate_title(frags: list[Fragment]) -> str:
        """Generate a human-readable title from common content words.

        Falls back to the earliest fragment's title (truncated) when no
        content words are available.

        Args:
            frags: Fragments in the cluster.

        Returns:
            A short title string.
        """
        counts: Counter[str] = Counter()
        for frag in frags:
            counts.update(_content_words(frag.title))
        if not counts:
            return frags[0].title[:50] if frags else "Untitled Thread"
        top = [word for word, _ in counts.most_common(_TITLE_TOP_WORDS)]
        return " ".join(word.capitalize() for word in top)

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
            merged = list(frag.threads)
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
        words_a = set(_content_words(a.title))
        words_b = set(_content_words(b.title))
        if not words_a or not words_b:
            return False
        union = words_a | words_b
        jaccard = len(words_a & words_b) / len(union)
        if jaccard < self.merge_jaccard:
            return False
        return bool(set(a.frequency_affinity) & set(b.frequency_affinity))
